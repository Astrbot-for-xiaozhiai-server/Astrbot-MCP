"""End-to-end tests for the remaining ``kb_*`` MCP tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from astrbot_mcp.server import build_server


@pytest.mark.asyncio
async def test_kb_learn_export_and_reindex(config_factory, payload, tmp_path: Path):
    config = config_factory(files={}, knowledge={})
    mcp = build_server(config)

    files_root = Path(config.services.files.root)
    (files_root / "notes").mkdir(parents=True, exist_ok=True)
    (files_root / "notes" / "learn.md").write_text(
        "# 标题\n\n深圳的天气通常很潮湿。", encoding="utf-8"
    )
    (files_root / "notes" / "empty.txt").write_text("", encoding="utf-8")
    (files_root / "notes" / "binary.bin").write_bytes(b"\x00\x01\x02")
    (files_root / "notes" / "data.bin").write_bytes(b"\x00binary")

    learned = payload(
        await mcp.call_tool(
            "kb_learn_file",
            {"path": "notes/learn.md", "tags": "file,research"},
        )
    )
    assert learned["created"] is True
    assert learned["source"] == "file:notes/learn.md"
    assert learned["title"] == "learn.md"
    assert learned["tags"] == "file,research"
    assert learned["learned_chars"] > 0

    # Re-learning unchanged content is a no-op.
    again = payload(
        await mcp.call_tool(
            "kb_learn_file",
            {"path": "notes/learn.md", "tags": "research,file"},
        )
    )
    assert again["unchanged"] is True

    listed = payload(await mcp.call_tool("kb_list", {"limit": 10}))
    assert listed["total"] == 1
    assert listed["documents"][0]["source"] == "file:notes/learn.md"
    assert listed["documents"][0]["chunks"] == 1

    fetched = payload(await mcp.call_tool("kb_get", {"key": "file:notes/learn.md"}))
    assert "深圳" in fetched["content"]

    exported = payload(await mcp.call_tool("kb_export", {}))
    assert exported["version"] == 1
    assert len(exported["documents"]) == 1
    assert "embedding" not in exported["chunks"][0]

    exported_file = payload(
        await mcp.call_tool(
            "kb_export", {"path": "dump.json", "include_embeddings": True}
        )
    )
    assert exported_file["exported"] is True
    dump = Path(exported_file["path"])
    assert dump.is_file()
    assert dump.parent == Path(config.services.knowledge.db_path).parent

    stats = payload(await mcp.call_tool("kb_stats", {}))
    if stats["fts_enabled"]:
        reindexed = payload(await mcp.call_tool("kb_reindex", {}))
        assert reindexed == {"reindexed": True, "chunks": 1}

    deleted = payload(await mcp.call_tool("kb_delete", {"key": "file:notes/learn.md"}))
    assert deleted["deleted"] is True
    assert payload(await mcp.call_tool("kb_list", {}))["total"] == 0


@pytest.mark.asyncio
async def test_kb_learn_file_rejects_bad_input(config_factory, tmp_path: Path):
    config = config_factory(files={}, knowledge={})
    mcp = build_server(config)
    files_root = Path(config.services.files.root)
    files_root.mkdir(parents=True, exist_ok=True)
    (files_root / "empty.txt").write_text("", encoding="utf-8")
    (files_root / "blob.dat").write_text("hello", encoding="utf-8")
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")

    for path in (
        "../outside.md",
        "missing.md",
        "blob.dat",
        "empty.txt",
        str(tmp_path / "outside.md"),
    ):
        with pytest.raises(ToolError):
            await mcp.call_tool("kb_learn_file", {"path": path})
