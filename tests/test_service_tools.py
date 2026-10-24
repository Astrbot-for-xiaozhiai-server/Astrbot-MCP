import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from astrbot_mcp.config import AppConfig
from astrbot_mcp.server import build_server

ALL_SERVICES = (
    "http_api",
    "web_search",
    "public_apis",
    "files",
    "sqlite",
    "github",
    "mail",
    "calendar",
    "knowledge",
)


def make_config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    for name in ALL_SERVICES:
        getattr(config.services, name).enabled = False

    config.services.http_api.enabled = True
    config.services.files.enabled = True
    config.services.files.root = str(tmp_path / "files")
    config.services.files.allow_write = True
    config.services.files.allow_delete = True

    config.services.sqlite.enabled = True
    config.services.sqlite.root = str(tmp_path / "sqlite")
    config.services.sqlite.allow_write = True

    config.services.calendar.enabled = True
    config.services.calendar.root = str(tmp_path / "calendar")
    config.services.calendar.allow_write = True

    config.services.knowledge.enabled = True
    config.services.knowledge.db_path = str(tmp_path / "knowledge.db")
    return config


def payload(result):
    # FastMCP 1.x returns (content_blocks, structured_content).
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict) and "result" in structured:
            structured = structured["result"]
            if isinstance(structured, str):
                return json.loads(structured)
            return structured
        result = content
    if isinstance(result, dict):
        return result
    for item in result:
        text = getattr(item, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"Unexpected tool result: {result!r}")


@pytest.mark.asyncio
async def test_file_and_sqlite_tools(tmp_path: Path):
    mcp = build_server(make_config(tmp_path))

    written = payload(
        await mcp.call_tool("fs_write", {"path": "notes/a.md", "content": "hello 世界"})
    )
    assert written["size"] == len("hello 世界".encode())

    read = payload(await mcp.call_tool("fs_read", {"path": "notes/a.md"}))
    assert read["content"] == "hello 世界"

    searched = payload(await mcp.call_tool("fs_search", {"query": "世界", "path": "."}))
    assert searched["count"] == 1

    # Symlinks escaping the sandbox must not be listed or searched.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (tmp_path / "files" / "leak.txt").symlink_to(secret)
    listed = payload(await mcp.call_tool("fs_list", {"path": "."}))
    assert all(entry["name"] != "leak.txt" for entry in listed["entries"])
    leaked = payload(
        await mcp.call_tool("fs_search", {"query": "TOP SECRET", "path": "."})
    )
    assert leaked["count"] == 0

    payload(
        await mcp.call_tool(
            "sqlite_execute",
            {
                "database": "demo.db",
                "sql": "CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT)",
            },
        )
    )
    payload(
        await mcp.call_tool(
            "sqlite_execute",
            {
                "database": "demo.db",
                "sql": "INSERT INTO items(name) VALUES(?)",
                "params": ["条目"],
            },
        )
    )
    queried = payload(
        await mcp.call_tool(
            "sqlite_query",
            {"database": "demo.db", "sql": "SELECT * FROM items"},
        )
    )
    assert queried["rows"] == [{"id": 1, "name": "条目"}]

    with pytest.raises(ToolError):
        await mcp.call_tool("fs_read", {"path": "../../etc/passwd"})


@pytest.mark.asyncio
async def test_calendar_tools(tmp_path: Path):
    mcp = build_server(make_config(tmp_path))
    start = (datetime.now().astimezone() + timedelta(days=1)).replace(microsecond=0)
    end = start + timedelta(hours=1)

    created = payload(
        await mcp.call_tool(
            "calendar_add_event",
            {
                "title": "测试日程",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
    )
    assert created["created"] is True

    listed = payload(await mcp.call_tool("calendar_list_events", {"days": 7}))
    assert listed["count"] == 1
    assert listed["events"][0]["summary"] == "测试日程"

    deleted = payload(
        await mcp.call_tool("calendar_delete_event", {"uid": created["uid"]})
    )
    assert deleted["deleted"] == 1
    after = payload(await mcp.call_tool("calendar_list_events", {"days": 7}))
    assert after["count"] == 0


@pytest.mark.asyncio
async def test_knowledge_tools(tmp_path: Path):
    mcp = build_server(make_config(tmp_path))

    remembered = payload(
        await mcp.call_tool(
            "kb_remember",
            {"content": "用户喜欢简洁的回答。", "tags": "preference"},
        )
    )
    assert remembered["remembered"] is True

    duplicate = payload(
        await mcp.call_tool(
            "kb_remember",
            {"content": "用户喜欢简洁的回答。", "tags": "preference"},
        )
    )
    assert duplicate["unchanged"] is True

    found = payload(
        await mcp.call_tool("kb_search", {"query": "简洁", "tags": "preference"})
    )
    assert found["count"] == 1
    assert found["results"][0]["source"].startswith("text:")

    stats = payload(await mcp.call_tool("kb_stats", {}))
    assert stats["documents"] == 1


@pytest.mark.asyncio
async def test_http_request_blocks_private_addresses(tmp_path: Path):
    mcp = build_server(make_config(tmp_path))
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "http_request",
            {"method": "GET", "url": "http://127.0.0.1:1/"},
        )
