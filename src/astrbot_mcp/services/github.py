"""GitHub REST API tools."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..utils import json_dumps, truncate_text


def _headers(ctx: AppContext) -> dict[str, str]:
    config = ctx.config.services.github
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    return headers


async def _request(
    ctx: AppContext,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    accept: str | None = None,
) -> Any:
    config = ctx.config.services.github
    headers = _headers(ctx)
    if accept:
        headers["Accept"] = accept
    url = config.base_url.rstrip("/") + path

    async with ctx.http_client(timeout=30, headers=headers) as client:
        response = await client.request(method, url, params=params, json=json_body)

    if response.status_code >= 400:
        detail = truncate_text(response.text, 1000)
        hint = ""
        if response.status_code in {401, 403} and not config.token:
            hint = " (set services.github.token / GITHUB_TOKEN)"
        raise ValueError(
            f"GitHub API {response.status_code} for {method} {path}{hint}: {detail}"
        )
    if response.status_code == 204:
        return None
    return response.json()


def _slim_repo(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "full_name": item.get("full_name"),
        "description": item.get("description"),
        "html_url": item.get("html_url"),
        "stars": item.get("stargazers_count"),
        "forks": item.get("forks_count"),
        "language": item.get("language"),
        "topics": item.get("topics", []),
        "updated_at": item.get("updated_at"),
        "archived": item.get("archived"),
    }


def _slim_issue(item: dict[str, Any]) -> dict[str, Any]:
    user = item.get("user") or {}
    labels = [
        label.get("name") for label in item.get("labels", []) if isinstance(label, dict)
    ]
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "author": user.get("login"),
        "labels": labels,
        "comments": item.get("comments"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "html_url": item.get("html_url"),
        "is_pull_request": "pull_request" in item,
    }


def register(mcp: FastMCP, ctx: AppContext) -> None:
    config = ctx.config.services.github

    @mcp.tool()
    async def github_search_repos(
        query: str,
        sort: str = "stars",
        per_page: int = 0,
    ) -> str:
        """搜索 GitHub 仓库。

        Args:
            query: GitHub 搜索语法，例如 "language:python mcp"。
            sort: 排序字段，可选 stars、forks、updated。
            per_page: 返回数量，1-100；0 表示使用配置值。
        """
        limit = max(1, min(int(per_page) or config.per_page, 100))
        data = await _request(
            ctx,
            "GET",
            "/search/repositories",
            params={
                "q": query,
                "sort": sort,
                "order": "desc",
                "per_page": limit,
            },
        )
        return json_dumps(
            {
                "total_count": data.get("total_count"),
                "items": [_slim_repo(item) for item in data.get("items", [])],
            }
        )

    @mcp.tool()
    async def github_get_repo(owner: str, repo: str) -> str:
        """获取 GitHub 仓库详情。

        Args:
            owner: 仓库拥有者。
            repo: 仓库名称。
        """
        data = await _request(ctx, "GET", f"/repos/{owner}/{repo}")
        result = _slim_repo(data)
        result.update(
            {
                "open_issues": data.get("open_issues_count"),
                "default_branch": data.get("default_branch"),
                "license": (data.get("license") or {}).get("spdx_id"),
                "homepage": data.get("homepage"),
            }
        )
        return json_dumps(result)

    @mcp.tool()
    async def github_list_issues(
        owner: str,
        repo: str,
        state: str = "open",
        labels: str = "",
        include_pulls: bool = False,
        per_page: int = 0,
    ) -> str:
        """列出仓库的 Issue（可选是否包含 Pull Request）。

        Args:
            owner: 仓库拥有者。
            repo: 仓库名称。
            state: open、closed 或 all。
            labels: 逗号分隔的标签过滤。
            include_pulls: 是否把 Pull Request 也包含在结果中。
            per_page: 返回数量，1-100；0 表示使用配置值。
        """
        limit = max(1, min(int(per_page) or config.per_page, 100))
        params: dict[str, Any] = {
            "state": state,
            "per_page": limit,
            "sort": "updated",
        }
        if labels:
            params["labels"] = labels
        data = await _request(
            ctx, "GET", f"/repos/{owner}/{repo}/issues", params=params
        )
        items = [
            _slim_issue(item)
            for item in data
            if include_pulls or "pull_request" not in item
        ]
        return json_dumps({"count": len(items), "items": items})

    @mcp.tool()
    async def github_get_issue(owner: str, repo: str, number: int) -> str:
        """获取单个 Issue 或 PR 的正文。

        Args:
            owner: 仓库拥有者。
            repo: 仓库名称。
            number: Issue 或 PR 编号。
        """
        item = await _request(ctx, "GET", f"/repos/{owner}/{repo}/issues/{number}")
        result = _slim_issue(item)
        result["body"] = truncate_text(str(item.get("body") or ""), 8000)
        return json_dumps(result)

    @mcp.tool()
    async def github_get_file(owner: str, repo: str, path: str, ref: str = "") -> str:
        """读取仓库中的文件内容。

        Args:
            owner: 仓库拥有者。
            repo: 仓库名称。
            path: 文件路径，例如 src/main.py。
            ref: 分支、标签或 commit SHA；留空使用默认分支。
        """
        encoded_path = quote(path, safe="/")
        params = {"ref": ref} if ref else None
        data = await _request(
            ctx,
            "GET",
            f"/repos/{owner}/{repo}/contents/{encoded_path}",
            params=params,
        )
        if isinstance(data, list):
            entries = [
                {
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "size": item.get("size"),
                }
                for item in data
            ]
            return json_dumps({"type": "directory", "path": path, "entries": entries})

        if data.get("encoding") not in {None, "base64"}:
            raise ValueError(
                f"GitHub returned unsupported encoding: {data.get('encoding')}"
            )
        content = str(data.get("content", "")).replace("\n", "")
        try:
            decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Failed to decode file content: {exc}") from exc
        return json_dumps(
            {
                "type": "file",
                "path": data.get("path"),
                "size": data.get("size"),
                "sha": data.get("sha"),
                "download_url": data.get("download_url"),
                "truncated": len(decoded) > 100_000,
                "content": truncate_text(decoded, 100_000),
            }
        )

    @mcp.tool()
    async def github_search_code(query: str, per_page: int = 20) -> str:
        """搜索 GitHub 代码（需要配置 token）。

        Args:
            query: 搜索语法，例如 "repo:AstrBotDevs/AstrBot mcp"。
            per_page: 返回数量，1-100。
        """
        if not config.token:
            raise ValueError("github_search_code requires services.github.token")
        limit = max(1, min(int(per_page), 100))
        data = await _request(
            ctx,
            "GET",
            "/search/code",
            params={"q": query, "per_page": limit},
        )
        items = [
            {
                "repository": (item.get("repository") or {}).get("full_name"),
                "path": item.get("path"),
                "html_url": item.get("html_url"),
            }
            for item in data.get("items", [])
        ]
        return json_dumps({"total_count": data.get("total_count"), "items": items})

    @mcp.tool()
    async def github_create_issue(
        owner: str,
        repo: str,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
    ) -> str:
        """创建 GitHub Issue（需开启 github.allow_write 且配置 token）。

        Args:
            owner: 仓库拥有者。
            repo: 仓库名称。
            title: Issue 标题。
            body: Issue 正文（Markdown）。
            labels: 标签名称列表。
        """
        if not config.allow_write:
            raise ValueError(
                "GitHub writes are disabled (services.github.allow_write=false)"
            )
        if not config.token:
            raise ValueError("github_create_issue requires services.github.token")
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        data = await _request(
            ctx, "POST", f"/repos/{owner}/{repo}/issues", json_body=payload
        )
        return json_dumps(
            {
                "number": data.get("number"),
                "title": data.get("title"),
                "state": data.get("state"),
                "html_url": data.get("html_url"),
            }
        )
