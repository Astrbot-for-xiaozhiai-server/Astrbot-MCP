import json

from astrbot_plugin.client import (
    make_tool_name,
    normalize_input_schema,
    parse_servers,
    result_to_text,
)


def test_parse_servers_merges_json_and_skips_disabled():
    config = {
        "servers": [
            {
                "name": "local",
                "transport": "stdio",
                "command": "python",
                "args_json": '["-m", "astrbot_mcp"]',
                "env_json": '{"FOO": "bar"}',
            },
            {
                "name": "off",
                "transport": "sse",
                "url": "https://example.com/sse",
                "enabled": False,
            },
        ],
        "servers_json": json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://example.com/mcp",
                        "headers_json": '{"Authorization": "Bearer x"}',
                    },
                    "local": {
                        "transport": "stdio",
                        "command": "uvx",
                        "args": ["some-server"],
                    },
                }
            }
        ),
    }

    specs = {spec.name: spec for spec in parse_servers(config)}

    assert set(specs) == {"local", "remote"}
    assert specs["local"].command == "uvx"
    assert specs["local"].args == ["some-server"]
    assert specs["remote"].transport == "streamable_http"
    assert specs["remote"].headers["Authorization"] == "Bearer x"


def test_parse_servers_ignores_invalid_json():
    specs = parse_servers(
        {
            "servers": [],
            "servers_json": "{not json}",
        }
    )
    assert specs == []


def test_parse_single_server_object_and_string_enabled():
    specs = parse_servers(
        {
            "name": "single",
            "transport": "streamable_http",
            "url": "https://example.com/mcp",
            "enabled": "false",
        }
    )
    assert specs == []

    specs = parse_servers(
        {"transport": "streamable_http", "url": "https://example.com/mcp"}
    )
    assert len(specs) == 1
    assert specs[0].name == "mcp"
    assert specs[0].transport == "streamable_http"


def test_make_tool_name_sanitizes_and_truncates():
    assert (
        make_tool_name("mcp", "my.server", "do:thing", 24) == "mcp_my_server_do_thing"
    )

    truncated = make_tool_name("mcp", "server", "a" * 100, 32)
    assert len(truncated) == 32
    assert all(ch.isalnum() or ch in "_-" for ch in truncated)


def test_normalize_input_schema():
    class Tool:
        inputSchema = {"properties": {"q": {"type": "string"}}}

    schema = normalize_input_schema(Tool())
    assert schema["type"] == "object"
    assert "q" in schema["properties"]


def test_result_to_text():
    class Item:
        type = "text"
        text = "hello"

    class Result:
        content = [Item()]
        isError = False

    assert result_to_text(Result()) == "hello"

    class ErrorResult(Result):
        isError = True

    assert result_to_text(ErrorResult()).startswith("[MCP error]")
