"""Embedding / vector-search tests for the knowledge base.

The OpenAI-compatible ``/embeddings`` endpoint is mocked with
``httpx.MockTransport`` so no network access is required.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from astrbot_mcp.config import EmbeddingConfig, KnowledgeConfig
from astrbot_mcp.server import build_server
from astrbot_mcp.services import knowledge as knowledge_module
from astrbot_mcp.services.knowledge import KnowledgeBase


def fake_vector(text: str) -> list[float]:
    """Deterministic 3-d embedding: one dimension per keyword."""
    return [float(text.count("猫")), float(text.count("狗")), 0.01]


def make_handler(
    requests: list[dict[str, Any]] | None = None,
    *,
    status: int = 200,
    reverse: bool = False,
    truncate_to: int | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer sk-test"
        payload = json.loads(request.content)
        if requests is not None:
            requests.append(payload)
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})

        data = [
            {"index": index, "embedding": fake_vector(text)}
            for index, text in enumerate(payload["input"])
        ]
        if truncate_to is not None:
            data = data[:truncate_to]
        if reverse:
            data.reverse()
        return httpx.Response(200, json={"data": data})

    return handler


@pytest.fixture
def patch_embeddings(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Replace ``httpx.AsyncClient`` inside the knowledge module only."""
    original = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            return original(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(knowledge_module.httpx, "AsyncClient", factory)

    return install


def make_kb(tmp_path: Path, **overrides: Any) -> KnowledgeBase:
    options: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "model": "text-embedding-3-small",
    }
    options.update(overrides)
    return KnowledgeBase(
        KnowledgeConfig(
            db_path=str(tmp_path / "kb.db"),
            chunk_size=100,
            chunk_overlap=0,
            embedding=EmbeddingConfig(**options),
        )
    )


# --------------------------------------------------------------- unit level
@pytest.mark.asyncio
async def test_vectors_stored_and_used_for_search(tmp_path: Path, patch_embeddings):
    requests: list[dict[str, Any]] = []
    patch_embeddings(make_handler(requests))
    kb = make_kb(tmp_path)

    await kb.add("doc:cat", "猫喜欢鱼。", tags="animal")
    await kb.add("doc:dog", "狗喜欢骨头。", tags="animal")

    assert len(requests) == 2
    assert requests[0]["model"] == "text-embedding-3-small"
    assert requests[0]["input"] == ["猫喜欢鱼。"]
    assert "dimensions" not in requests[0]

    stats = await kb.stats()
    assert stats["embedded_chunks"] == 2
    assert stats["embedding_enabled"] is True

    # "猫咪" appears in no chunk: only the vector index can find doc:cat.
    # Every embedded chunk is scored, so the closest one must rank first.
    result = await kb.search("猫咪")
    assert result["keyword_hits"] == 0
    assert result["vector_hits"] == 2
    assert result["results"][0]["source"] == "doc:cat"
    assert result["results"][1]["source"] == "doc:dog"

    filtered = await kb.search("猫咪", tags="other")
    assert filtered["vector_hits"] == 0
    assert filtered["count"] == 0


@pytest.mark.asyncio
async def test_embeddings_are_batched_in_groups_of_64(tmp_path: Path, patch_embeddings):
    requests: list[dict[str, Any]] = []
    patch_embeddings(make_handler(requests))
    kb = make_kb(tmp_path, dimensions=8)

    content = "\n\n".join(["x" * 100] * 65)
    added = await kb.add("doc:long", content)
    assert added["chunks"] == 65

    assert [len(item["input"]) for item in requests] == [64, 1]
    assert all(item["dimensions"] == 8 for item in requests)
    assert (await kb.stats())["embedded_chunks"] == 65


@pytest.mark.asyncio
async def test_embeddings_follow_response_index_order(tmp_path: Path, patch_embeddings):
    patch_embeddings(make_handler(reverse=True))
    kb = make_kb(tmp_path)

    await kb.add("doc:mixed", "猫" * 100 + "\n\n" + "狗" * 100)

    connection = sqlite3.connect(tmp_path / "kb.db")
    try:
        rows = connection.execute(
            "SELECT ordinal, embedding FROM kb_chunks ORDER BY ordinal"
        ).fetchall()
    finally:
        connection.close()

    assert [row[0] for row in rows] == [0, 1]
    assert json.loads(rows[0][1]) == [100.0, 0.0, 0.01]
    assert json.loads(rows[1][1]) == [0.0, 100.0, 0.01]


@pytest.mark.asyncio
async def test_embedding_failure_keeps_keyword_search(tmp_path: Path, patch_embeddings):
    patch_embeddings(make_handler(status=500))
    kb = make_kb(tmp_path)

    added = await kb.add("doc:cat", "猫喜欢鱼。")
    assert added["created"] is True
    assert (await kb.stats())["embedded_chunks"] == 0

    result = await kb.search("猫喜欢鱼")
    assert result["keyword_hits"] == 1
    assert result["vector_hits"] == 0
    assert result["results"][0]["source"] == "doc:cat"


@pytest.mark.asyncio
async def test_embedding_skipped_without_api_key(tmp_path: Path, patch_embeddings):
    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP request should be made without an API key")

    patch_embeddings(explode)
    kb = make_kb(tmp_path, api_key="")

    await kb.add("doc:cat", "猫喜欢鱼。")
    stats = await kb.stats()
    assert stats["embedded_chunks"] == 0
    assert stats["embedding_enabled"] is True
    assert (await kb.search("猫喜欢鱼"))["count"] == 1


@pytest.mark.asyncio
async def test_embedding_count_mismatch_is_ignored(tmp_path: Path, patch_embeddings):
    patch_embeddings(make_handler(truncate_to=1))
    kb = make_kb(tmp_path)

    # Two chunks but only one returned vector -> no embeddings are stored.
    await kb.add("doc:mixed", "猫" * 100 + "\n\n" + "狗" * 100)
    stats = await kb.stats()
    assert stats["chunks"] == 2
    assert stats["embedded_chunks"] == 0


# ------------------------------------------------------------- MCP tool level
@pytest.mark.asyncio
async def test_vector_search_through_mcp_tools(
    config_factory, payload, patch_embeddings
):
    patch_embeddings(make_handler())
    config = config_factory(
        knowledge={
            "embedding": EmbeddingConfig(
                enabled=True,
                base_url="https://api.example.com/v1",
                api_key="sk-test",
            )
        }
    )
    mcp = build_server(config)

    for source, text in (("doc:cat", "猫喜欢鱼。"), ("doc:dog", "狗喜欢骨头。")):
        added = payload(
            await mcp.call_tool("kb_add", {"source": source, "content": text})
        )
        assert added["created"] is True

    found = payload(await mcp.call_tool("kb_search", {"query": "猫咪"}))
    assert found["keyword_hits"] == 0
    assert found["vector_hits"] == 2
    assert found["results"][0]["source"] == "doc:cat"

    stats = payload(await mcp.call_tool("kb_stats", {}))
    assert stats["embedded_chunks"] == 2
