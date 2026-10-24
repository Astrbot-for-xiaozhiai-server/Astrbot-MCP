"""Offline tests for the HTTP, search, public-API, GitHub and mail services."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from astrbot_mcp.server import build_server
from astrbot_mcp.services import mail as mail_service


async def _allow_all(*args, **kwargs) -> None:
    return None


@pytest.fixture
def offline_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip DNS resolution so tests never touch the network."""
    monkeypatch.setattr("astrbot_mcp.net.validate_url", _allow_all)


# --------------------------------------------------------------------- HTTP
@pytest.mark.asyncio
async def test_http_request_parses_json(
    config_factory, payload, patch_http, offline_ssrf
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"content-type": "application/json"},
        )

    patch_http(handler)
    mcp = build_server(config_factory(http_api={}))
    data = payload(
        await mcp.call_tool(
            "http_request",
            {"method": "GET", "url": "https://api.example.com/json"},
        )
    )
    assert data["status"] == 200
    assert data["json"] == {"ok": True}


@pytest.mark.asyncio
async def test_http_request_truncates_and_follows_redirects(
    config_factory, payload, patch_http, offline_ssrf
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "https://api.example.com/big"}
            )
        return httpx.Response(
            200, content=b"x" * 4096, headers={"content-type": "text/plain"}
        )

    patch_http(handler)
    mcp = build_server(config_factory(http_api={}))
    data = payload(
        await mcp.call_tool(
            "http_request",
            {
                "method": "GET",
                "url": "https://api.example.com/start",
                "max_bytes": 1024,
            },
        )
    )
    assert data["url"] == "https://api.example.com/big"
    assert data["truncated"] is True
    assert len(data["body"]) == 1024


@pytest.mark.asyncio
async def test_http_request_rejects_conflicting_bodies(
    config_factory, patch_http, offline_ssrf
):
    patch_http(lambda request: httpx.Response(200))
    mcp = build_server(config_factory(http_api={}))
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "http_request",
            {
                "method": "POST",
                "url": "https://api.example.com/",
                "json_body": {"a": 1},
                "body": "a=1",
            },
        )


# ---------------------------------------------------------------- web search
DDG_HTML = """
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
    第一个结果
  </a>
  <a class="result__snippet">这是摘要</a>
</div>
"""

DDG_LITE = """
<table>
  <tr><td><a class="result-link" href="https://example.org/b">Lite 结果</a></td></tr>
  <tr><td class="result-snippet">Lite 摘要</td></tr>
</table>
"""


@pytest.mark.asyncio
async def test_web_search_duckduckgo_and_lite_fallback(
    config_factory, payload, patch_http
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(
                202, content=b"", headers={"content-type": "text/html"}
            )
        return httpx.Response(200, text=DDG_LITE, headers={"content-type": "text/html"})

    patch_http(handler)
    mcp = build_server(config_factory(web_search={"provider": "duckduckgo"}))
    data = payload(await mcp.call_tool("web_search", {"query": "AstrBot"}))
    assert data["provider"] == "duckduckgo"
    assert data["count"] == 1
    assert data["results"][0]["url"] == "https://example.org/b"
    assert data["results"][0]["snippet"] == "Lite 摘要"


@pytest.mark.asyncio
async def test_web_search_duckduckgo_unwraps_redirect_url(
    config_factory, payload, patch_http
):
    patch_http(
        lambda request: httpx.Response(
            200, text=DDG_HTML, headers={"content-type": "text/html"}
        )
    )
    mcp = build_server(config_factory(web_search={}))
    data = payload(
        await mcp.call_tool("web_search", {"query": "测试", "max_results": 3})
    )
    assert data["results"][0]["url"] == "https://example.com/a"
    assert data["results"][0]["title"] == "第一个结果"


@pytest.mark.asyncio
async def test_web_search_tavily_and_missing_keys(config_factory, payload, patch_http):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"].startswith("application/json")
        return httpx.Response(
            200,
            json={
                "answer": "直接回答",
                "results": [
                    {
                        "title": "结果标题",
                        "url": "https://example.com/r",
                        "content": "内容",
                        "score": 0.9,
                    }
                ],
            },
        )

    patch_http(handler)
    mcp = build_server(
        config_factory(web_search={"provider": "tavily", "api_key": "tvly-x"})
    )
    data = payload(await mcp.call_tool("web_search", {"query": "AstrBot"}))
    assert data["count"] == 2
    assert data["results"][0]["title"] == "Tavily answer"

    no_key = build_server(config_factory(web_search={"provider": "tavily"}))
    with pytest.raises(ToolError):
        await no_key.call_tool("web_search", {"query": "AstrBot"})


@pytest.mark.asyncio
async def test_web_search_bing_and_searxng(config_factory, payload, patch_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.bing.microsoft.com":
            assert request.headers["ocp-apim-subscription-key"] == "bing-key"
            return httpx.Response(
                200,
                json={
                    "webPages": {
                        "value": [
                            {
                                "name": "Bing 结果",
                                "url": "https://b.example",
                                "snippet": "S",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Searx", "url": "https://s.example", "content": "C"}
                ]
            },
        )

    patch_http(handler)

    bing = build_server(
        config_factory(web_search={"provider": "bing", "api_key": "bing-key"})
    )
    bing_data = payload(await bing.call_tool("web_search", {"query": "q"}))
    assert bing_data["results"][0]["title"] == "Bing 结果"

    searx = build_server(
        config_factory(
            web_search={"provider": "searxng", "base_url": "https://searx.example"}
        )
    )
    searx_data = payload(await searx.call_tool("web_search", {"query": "q"}))
    assert searx_data["results"][0]["url"] == "https://s.example"


@pytest.mark.asyncio
async def test_web_search_rejects_unknown_provider(config_factory):
    mcp = build_server(config_factory(web_search={}))
    with pytest.raises(ToolError):
        await mcp.call_tool("web_search", {"query": "q", "provider": "google"})


@pytest.mark.asyncio
async def test_web_fetch_extracts_text_and_links(
    config_factory, payload, patch_http, offline_ssrf
):
    html = """
    <html><head><title>示例文档</title></head>
      <body><article><p>第一段</p><p>第二段</p>
        <a href="/next">下一页</a></article></body>
    </html>
    """
    patch_http(
        lambda request: httpx.Response(
            200, text=html, headers={"content-type": "text/html; charset=utf-8"}
        )
    )
    mcp = build_server(config_factory(web_search={}))
    data = payload(
        await mcp.call_tool(
            "web_fetch",
            {
                "url": "https://docs.example.com/page",
                "include_links": True,
            },
        )
    )
    assert data["title"] == "示例文档"
    assert "第一段" in data["text"]
    assert data["links"] == [{"url": "https://docs.example.com/next", "text": "下一页"}]


# --------------------------------------------------------------- public APIs
@pytest.mark.asyncio
async def test_get_time(config_factory, payload):
    mcp = build_server(config_factory(public_apis={}))
    data = payload(await mcp.call_tool("get_time", {"timezone": "Asia/Shanghai"}))
    assert data["timezone"] == "Asia/Shanghai"
    assert data["weekday"].startswith("星期")

    with pytest.raises(ToolError):
        await mcp.call_tool("get_time", {"timezone": "Not/AZone"})


@pytest.mark.asyncio
async def test_convert_currency(config_factory, payload, patch_http):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v6/latest/USD"
        return httpx.Response(
            200,
            json={
                "result": "success",
                "rates": {"CNY": 7.2, "JPY": 150.0},
                "time_last_update_utc": "Fri, 24 Oct 2026 00:00:00 +0000",
            },
        )

    patch_http(handler)
    mcp = build_server(config_factory(public_apis={}))
    data = payload(
        await mcp.call_tool(
            "convert_currency",
            {"amount": 10, "from_currency": "usd", "to_currency": "cny"},
        )
    )
    assert data["from"] == "USD"
    assert data["converted"] == 72.0

    with pytest.raises(ToolError):
        await mcp.call_tool(
            "convert_currency",
            {"amount": 1, "from_currency": "US", "to_currency": "CNY"},
        )
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "convert_currency",
            {"amount": 1, "from_currency": "USD", "to_currency": "XXX"},
        )


@pytest.mark.asyncio
async def test_get_weather(config_factory, payload, patch_http):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "北京",
                            "admin1": "北京市",
                            "country": "中国",
                            "latitude": 39.9,
                            "longitude": 116.4,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "current": {
                    "time": "2026-10-24T12:00",
                    "weather_code": 0,
                    "temperature_2m": 20.5,
                    "apparent_temperature": 19.0,
                    "relative_humidity_2m": 40,
                    "precipitation": 0,
                    "wind_speed_10m": 8,
                    "is_day": 1,
                },
                "daily": {
                    "time": ["2026-10-24"],
                    "weather_code": [61],
                    "temperature_2m_max": [22.0],
                    "temperature_2m_min": [10.0],
                    "precipitation_sum": [3.2],
                    "precipitation_probability_max": [70],
                    "sunrise": ["2026-10-24T06:30"],
                    "sunset": ["2026-10-24T17:20"],
                },
            },
        )

    patch_http(handler)
    mcp = build_server(config_factory(public_apis={}))
    data = payload(await mcp.call_tool("get_weather", {"location": "北京"}))
    assert data["location"]["name"] == "北京"
    assert data["current"]["condition"] == "晴"
    assert data["daily"][0]["condition"] == "小雨"
    assert data["daily"][0]["temperature_max_c"] == 22.0

    empty = build_server(config_factory(public_apis={}))
    patch_http(lambda request: httpx.Response(200, json={"results": []}))
    with pytest.raises(ToolError):
        await empty.call_tool("get_weather", {"location": "Atlantis"})


# ------------------------------------------------------------------- GitHub
def _github_handler(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/search/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "items": [
                        {
                            "full_name": "owner/repo",
                            "description": "demo",
                            "html_url": "https://github.com/owner/repo",
                            "stargazers_count": 42,
                            "forks_count": 7,
                            "language": "Python",
                            "topics": ["mcp"],
                            "updated_at": "2026-10-01T00:00:00Z",
                            "archived": False,
                        }
                    ],
                },
            )
        if path == "/repos/owner/repo/contents/README.md":
            content = base64.b64encode(b"hello world").decode("ascii")
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": content,
                    "path": "README.md",
                    "size": 11,
                    "sha": "abc",
                    "download_url": "https://raw.example/README.md",
                },
            )
        if path == "/repos/owner/repo/contents/docs":
            return httpx.Response(
                200,
                json=[
                    {"name": "a.md", "type": "file", "size": 10},
                    {"name": "sub", "type": "dir", "size": 0},
                ],
            )
        if path == "/repos/owner/repo/issues":
            return httpx.Response(
                201,
                json={
                    "number": 5,
                    "title": "Bug",
                    "state": "open",
                    "html_url": "https://github.com/owner/repo/issues/5",
                },
            )
        if path == "/repos/owner/missing":
            return httpx.Response(404, text='{"message":"Not Found"}')
        return httpx.Response(500, text="unexpected path")

    return handler


@pytest.mark.asyncio
async def test_github_read_tools(config_factory, payload, patch_http):
    requests: list[httpx.Request] = []
    patch_http(_github_handler(requests))
    mcp = build_server(config_factory(github={"token": "gh-token"}))

    repos = payload(
        await mcp.call_tool("github_search_repos", {"query": "mcp", "per_page": 5})
    )
    assert repos["total_count"] == 1
    assert repos["items"][0]["stars"] == 42
    assert requests[0].headers["authorization"] == "Bearer gh-token"

    file_data = payload(
        await mcp.call_tool(
            "github_get_file",
            {"owner": "owner", "repo": "repo", "path": "README.md"},
        )
    )
    assert file_data["content"] == "hello world"
    assert file_data["type"] == "file"

    directory = payload(
        await mcp.call_tool(
            "github_get_file",
            {"owner": "owner", "repo": "repo", "path": "docs"},
        )
    )
    assert directory["type"] == "directory"
    assert [entry["name"] for entry in directory["entries"]] == ["a.md", "sub"]


@pytest.mark.asyncio
async def test_github_errors_and_write_guard(config_factory, payload, patch_http):
    requests: list[httpx.Request] = []
    patch_http(_github_handler(requests))
    mcp = build_server(config_factory(github={}))
    with pytest.raises(ToolError):
        await mcp.call_tool("github_get_repo", {"owner": "owner", "repo": "missing"})

    discovered = payload(await mcp.call_tool("github_search_repos", {"query": "mcp"}))
    assert discovered["items"][0]["full_name"] == "owner/repo"

    # Writes stay disabled unless explicitly enabled *and* a token is present.
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "github_create_issue",
            {"owner": "owner", "repo": "repo", "title": "Bug"},
        )

    writable = build_server(
        config_factory(github={"token": "gh-token", "allow_write": True})
    )
    created = payload(
        await writable.call_tool(
            "github_create_issue",
            {"owner": "owner", "repo": "repo", "title": "Bug", "labels": ["bug"]},
        )
    )
    assert created["number"] == 5
    body = json.loads(requests[-1].content)
    assert body == {"title": "Bug", "body": "", "labels": ["bug"]}


# --------------------------------------------------------------------- mail
@pytest.mark.asyncio
async def test_mail_send_uses_configured_smtp(config_factory, payload, monkeypatch):
    captured: dict = {}

    def fake_send(config, message, recipients) -> None:
        captured["config"] = config
        captured["message"] = message
        captured["recipients"] = recipients

    monkeypatch.setattr(mail_service, "_send_sync", fake_send)
    mcp = build_server(
        config_factory(
            mail={
                "smtp_host": "smtp.example.com",
                "username": "bot@example.com",
                "from_addr": "bot@example.com",
            }
        )
    )
    data = payload(
        await mcp.call_tool(
            "mail_send",
            {
                "to": "a@example.com; b@example.com",
                "cc": "c@example.com",
                "bcc": "d@example.com",
                "subject": "主题",
                "body": "正文",
                "html_body": "<p>正文</p>",
            },
        )
    )
    assert data["sent"] is True
    assert data["to"] == ["a@example.com", "b@example.com"]
    assert captured["recipients"] == [
        "a@example.com",
        "b@example.com",
        "c@example.com",
        "d@example.com",
    ]
    message = captured["message"]
    assert message["Subject"] == "主题"
    assert message.get_content_type() == "multipart/alternative"


@pytest.mark.asyncio
async def test_mail_tools_require_configuration(config_factory):
    mcp = build_server(config_factory(mail={}))
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "mail_send",
            {"to": "a@example.com", "subject": "s", "body": "b"},
        )
    with pytest.raises(ToolError):
        await mcp.call_tool("mail_list", {})
    with pytest.raises(ToolError):
        await mcp.call_tool("mail_read", {})


def test_mail_helpers():
    assert mail_service._split_addresses("a@x.com, b@x.com; c@x.com") == [
        "a@x.com",
        "b@x.com",
        "c@x.com",
    ]
    assert mail_service._decode_header("=?utf-8?b?5L2g5aW9?=") == "你好"

    html = b"<html><body><p>Hello</p></body></html>"
    message = mail_service.email.message.EmailMessage()
    message.set_content(html.decode(), subtype="html")
    assert "Hello" in mail_service._extract_body(message)
