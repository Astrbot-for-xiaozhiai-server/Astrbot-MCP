from pathlib import Path

import pytest

from astrbot_mcp.config import KnowledgeConfig
from astrbot_mcp.services.knowledge import KnowledgeBase, chunk_text


def make_kb(tmp_path: Path) -> KnowledgeBase:
    return KnowledgeBase(
        KnowledgeConfig(
            db_path=str(tmp_path / "kb.db"),
            chunk_size=200,
            chunk_overlap=40,
        )
    )


def test_chunk_text_overlap():
    chunks = chunk_text("a" * 600, 200, 40)
    assert len(chunks) >= 3
    assert chunks[1][:40] == chunks[0][-40:]


def test_chunk_text_prefers_paragraphs():
    text = "第一段内容。\n\n第二段内容。\n\n第三段内容。"
    chunks = chunk_text(text, 200, 20)
    assert chunks == [text]


@pytest.mark.asyncio
async def test_knowledge_crud_and_search(tmp_path: Path):
    kb = make_kb(tmp_path)

    created = await kb.add(
        "doc:1",
        "AstrBot 支持 MCP 协议。知识库可以自动学习网页内容。",
        title="AstrBot 笔记",
        tags="astrbot,docs",
    )
    assert created["created"] is True
    assert created["chunks"] == 1

    unchanged = await kb.add(
        "doc:1",
        "AstrBot 支持 MCP 协议。知识库可以自动学习网页内容。",
        title="AstrBot 笔记",
        tags="astrbot,docs",
    )
    assert unchanged["unchanged"] is True

    result = await kb.search("自动学习")
    assert result["count"] >= 1
    assert result["results"][0]["source"] == "doc:1"

    filtered = await kb.search("AstrBot", tags="docs")
    assert filtered["count"] >= 1
    empty = await kb.search("AstrBot", tags="nonexistent")
    assert empty["count"] == 0

    stats = await kb.stats()
    assert stats["documents"] == 1
    assert stats["chunks"] == 1

    listed = await kb.list_documents()
    assert listed["total"] == 1
    assert listed["documents"][0]["title"] == "AstrBot 笔记"

    fetched = await kb.get("doc:1")
    assert "MCP" in fetched["content"]

    deleted = await kb.delete("doc:1")
    assert deleted["deleted"] is True
    assert (await kb.stats())["documents"] == 0


@pytest.mark.asyncio
async def test_knowledge_remember_is_idempotent(tmp_path: Path):
    kb = make_kb(tmp_path)
    first = await kb.add("note:1", "用户偏好使用中文回答。")
    second = await kb.add("note:1", "用户偏好使用中文回答。")
    assert first["created"] is True
    assert second["unchanged"] is True

    tagged = await kb.add("note:1", "用户偏好使用中文回答。", tags="preference")
    assert tagged["unchanged"] is False
    assert tagged["tags"] == "preference"
