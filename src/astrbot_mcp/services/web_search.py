"""Web search and page fetching tools."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..net import safe_request
from ..utils import json_dumps, strip_html, truncate_text

_MAX_RESULT_TITLE = 300
_MAX_SNIPPET = 800

# DuckDuckGo serves an empty 202 challenge page to non-browser clients.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DDG_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _unwrap_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        query = parse_qs(parsed.query)
        target = query.get("uddg")
        if target:
            return unquote(target[0])
    return url


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    for block in soup.select("div.result"):
        anchor = block.select_one("a.result__a")
        if anchor is None:
            continue
        snippet = block.select_one(".result__snippet")
        results.append(
            {
                "title": anchor.get_text(" ", strip=True)[:_MAX_RESULT_TITLE],
                "url": _unwrap_duckduckgo_url(str(anchor.get("href", ""))),
                "snippet": (
                    snippet.get_text(" ", strip=True)[:_MAX_SNIPPET]
                    if snippet is not None
                    else ""
                ),
            }
        )
    return results


def _parse_ddg_lite(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    for anchor in soup.select("a.result-link"):
        row = anchor.find_parent("tr")
        snippet = ""
        if row is not None:
            next_row = row.find_next_sibling("tr")
            if next_row is not None:
                cell = next_row.select_one("td.result-snippet")
                if cell is not None:
                    snippet = cell.get_text(" ", strip=True)[:_MAX_SNIPPET]
        results.append(
            {
                "title": anchor.get_text(" ", strip=True)[:_MAX_RESULT_TITLE],
                "url": _unwrap_duckduckgo_url(str(anchor.get("href", ""))),
                "snippet": snippet,
            }
        )
    return results


async def _search_duckduckgo(
    ctx: AppContext, query: str, limit: int, timeout: float
) -> list[dict[str, str]]:
    async with ctx.http_client(timeout=timeout, headers=_DDG_HEADERS) as client:
        response = await client.post(
            "https://html.duckduckgo.com/html/", data={"q": query}
        )
        results = _parse_ddg_html(response.text) if response.status_code == 200 else []

        if not results:
            lite = await client.post(
                "https://lite.duckduckgo.com/lite/", data={"q": query}
            )
            if lite.status_code == 200:
                results = _parse_ddg_lite(lite.text)

        if not results:
            if response.status_code != 200:
                raise ValueError(
                    "DuckDuckGo blocked this request "
                    f"(HTTP {response.status_code}). Configure tavily, bing or "
                    "searxng in services.web_search for reliable search."
                )
            raise ValueError(
                "DuckDuckGo returned no parseable results. Configure tavily, bing "
                "or searxng in services.web_search for reliable search."
            )

    return results[:limit]


async def _search_tavily(
    ctx: AppContext, query: str, limit: int, timeout: float
) -> list[dict[str, str]]:
    config = ctx.config.services.web_search
    if not config.api_key:
        raise ValueError(
            "web_search.provider=tavily requires web_search.api_key (TAVILY_API_KEY)"
        )
    payload = {
        "api_key": config.api_key,
        "query": query,
        "max_results": limit,
        "search_depth": "basic",
        "include_answer": True,
    }
    async with ctx.http_client(timeout=timeout) as client:
        response = await client.post("https://api.tavily.com/search", json=payload)
        response.raise_for_status()
        data = response.json()

    results: list[dict[str, Any]] = []
    for item in data.get("results", []):
        results.append(
            {
                "title": str(item.get("title", ""))[:_MAX_RESULT_TITLE],
                "url": str(item.get("url", "")),
                "snippet": str(item.get("content", ""))[:_MAX_SNIPPET],
                "score": item.get("score"),
            }
        )
    answer = data.get("answer")
    if answer:
        results.insert(0, {"title": "Tavily answer", "url": "", "snippet": str(answer)})
    return results[:limit]


async def _search_bing(
    ctx: AppContext, query: str, limit: int, timeout: float
) -> list[dict[str, str]]:
    config = ctx.config.services.web_search
    if not config.api_key:
        raise ValueError(
            "web_search.provider=bing requires web_search.api_key (BING_SEARCH_API_KEY)"
        )
    async with ctx.http_client(
        timeout=timeout, headers={"Ocp-Apim-Subscription-Key": config.api_key}
    ) as client:
        response = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={
                "q": query,
                "count": limit,
                "mkt": "zh-CN",
                "textDecorations": False,
            },
        )
        response.raise_for_status()
        data = response.json()

    results: list[dict[str, str]] = []
    for item in data.get("webPages", {}).get("value", []):
        results.append(
            {
                "title": str(item.get("name", ""))[:_MAX_RESULT_TITLE],
                "url": str(item.get("url", "")),
                "snippet": str(item.get("snippet", ""))[:_MAX_SNIPPET],
            }
        )
    return results[:limit]


async def _search_searxng(
    ctx: AppContext, query: str, limit: int, timeout: float
) -> list[dict[str, str]]:
    config = ctx.config.services.web_search
    if not config.base_url:
        raise ValueError(
            "web_search.provider=searxng requires web_search.base_url "
            "(e.g. https://searx.example.com)"
        )
    endpoint = config.base_url.rstrip("/") + "/search"
    async with ctx.http_client(timeout=timeout) as client:
        response = await client.get(
            endpoint,
            params={
                "q": query,
                "format": "json",
                "language": "zh-CN",
                "safesearch": 1,
            },
        )
        response.raise_for_status()
        data = response.json()

    results: list[dict[str, str]] = []
    for item in data.get("results", []):
        results.append(
            {
                "title": str(item.get("title", ""))[:_MAX_RESULT_TITLE],
                "url": str(item.get("url", "")),
                "snippet": str(item.get("content", ""))[:_MAX_SNIPPET],
            }
        )
    return results[:limit]


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    async def web_search(
        query: str,
        max_results: int = 8,
        provider: str = "",
    ) -> str:
        """搜索网页并返回标题、链接与摘要。

        Args:
            query: 搜索关键词。
            max_results: 返回结果数量，1-20。
            provider: 临时覆盖配置的搜索后端，可选 duckduckgo、tavily、bing、searxng；留空使用配置值。
        """
        config = ctx.config.services.web_search
        limit = max(1, min(int(max_results), 20))
        selected = (provider or config.provider).strip().lower()

        if selected == "duckduckgo":
            results = await _search_duckduckgo(ctx, query, limit, config.timeout)
        elif selected == "tavily":
            results = await _search_tavily(ctx, query, limit, config.timeout)
        elif selected == "bing":
            results = await _search_bing(ctx, query, limit, config.timeout)
        elif selected == "searxng":
            results = await _search_searxng(ctx, query, limit, config.timeout)
        else:
            raise ValueError(f"Unsupported search provider: {selected}")

        return json_dumps(
            {
                "query": query,
                "provider": selected,
                "count": len(results),
                "results": results,
            }
        )

    @mcp.tool()
    async def web_fetch(
        url: str,
        max_chars: int = 8000,
        include_links: bool = False,
        max_links: int = 30,
    ) -> str:
        """抓取网页并提取可读正文，用于阅读搜索结果或在线文档。

        Args:
            url: 要抓取的 http/https URL。
            max_chars: 正文字符上限，100-50000。
            include_links: 是否同时返回页面中的链接。
            max_links: include_links=true 时最多返回多少条链接。
        """
        limit = max(100, min(int(max_chars), 50_000))
        link_limit = max(0, min(int(max_links), 200)) if include_links else 0

        response = await safe_request(
            ctx,
            "GET",
            url,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=ctx.config.security.request_timeout,
            follow_redirects=True,
        )

        content_type = response.content_type
        result: dict[str, Any] = {
            "url": response.url,
            "status": response.status,
            "content_type": content_type,
            "truncated": response.truncated,
        }

        if "html" in content_type or not content_type:
            extracted = strip_html(
                response.text(),
                base_url=response.url,
                max_links=link_limit,
            )
            result.update(
                {
                    "title": extracted["title"],
                    "text": truncate_text(extracted["text"], limit),
                }
            )
            if include_links:
                result["links"] = extracted["links"]
        else:
            result["text"] = truncate_text(response.text(), limit)

        return json_dumps(result)
