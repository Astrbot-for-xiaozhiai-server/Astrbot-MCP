"""Generic HTTP/REST request tool."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..net import safe_request
from ..utils import json_dumps


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    async def http_request(
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        body: str | None = None,
        timeout: float = 30.0,
        max_bytes: int = 200_000,
        follow_redirects: bool = True,
    ) -> str:
        """发送任意 HTTP/REST 请求，用于访问未单独接入的开放 API。

        Args:
            method: HTTP 方法，支持 GET、POST、PUT、PATCH、DELETE、HEAD、OPTIONS。
            url: 完整 URL，必须是 http:// 或 https:// 开头。
            headers: 附加请求头，例如 {"Authorization": "Bearer ..."}。
            params: URL 查询参数对象。
            json_body: 以 JSON 发送的请求体（对象或数组）。
            body: 原始字符串请求体，与 json_body 二选一。
            timeout: 请求超时秒数。
            max_bytes: 最多读取的响应字节数，超出会被截断。
            follow_redirects: 是否跟随重定向；每一跳都会重新执行安全检查。
        """
        if json_body is not None and body is not None:
            raise ValueError("json_body and body are mutually exclusive")
        if max_bytes < 1024:
            raise ValueError("max_bytes must be at least 1024")

        response = await safe_request(
            ctx,
            method,
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            content=body,
            timeout=timeout,
            max_bytes=max_bytes,
            allow_private=ctx.config.services.http_api.allow_private_networks,
            follow_redirects=follow_redirects,
        )

        text = response.text()
        payload: dict[str, Any] = {
            "status": response.status,
            "url": response.url,
            "content_type": response.content_type,
            "truncated": response.truncated,
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key not in {"set-cookie", "set-cookie2"}
            },
            "body": text,
        }

        if "json" in response.content_type and not response.truncated:
            try:
                payload["json"] = json.loads(text)
            except (ValueError, TypeError):
                pass

        return json_dumps(payload)
