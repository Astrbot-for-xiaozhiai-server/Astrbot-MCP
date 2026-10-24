"""FastMCP server construction and transport handling."""

from __future__ import annotations

import hmac
import json
import logging

from mcp.server.fastmcp import FastMCP

from .config import AppConfig
from .context import AppContext
from .registry import register_all

logger = logging.getLogger("astrbot_mcp")


class GatewayMiddleware:
    """Adds ``/health`` and optional bearer-token auth to the MCP ASGI app."""

    def __init__(
        self, app, *, auth_token: str = "", health: dict | None = None
    ) -> None:
        self.app = app
        self.auth_token = auth_token
        self.health = health or {"status": "ok"}

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.rstrip("/") in {"/health", "/healthz"}:
            body = json.dumps(self.health, ensure_ascii=False).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        if self.auth_token:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            expected = f"Bearer {self.auth_token}"
            provided = headers.get("authorization", "")
            if not hmac.compare_digest(
                provided.encode("utf-8"), expected.encode("utf-8")
            ):
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b'Bearer realm="astrbot-mcp"'),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)


def build_server(config: AppConfig) -> FastMCP:
    """Create the FastMCP instance and register every enabled service."""
    server_config = config.server
    mcp = FastMCP(
        name=server_config.name,
        instructions=server_config.instructions,
        host=server_config.host,
        port=server_config.port,
        sse_path=server_config.sse_path,
        message_path=server_config.message_path,
        streamable_http_path=server_config.streamable_http_path,
        mount_path=server_config.mount_path,
        stateless_http=server_config.stateless_http,
        json_response=server_config.json_response,
        log_level=server_config.log_level,
    )

    ctx = AppContext(config)
    registered = register_all(mcp, ctx)
    logger.info("Enabled services: %s", ", ".join(registered) or "none")
    return mcp


def _asgi_app(mcp: FastMCP, config: AppConfig):
    if config.server.transport == "sse":
        return mcp.sse_app(mount_path=config.server.mount_path)
    return mcp.streamable_http_app()


def run_server(config: AppConfig) -> None:
    """Run the server using the configured transport."""
    server_config = config.server
    mcp = build_server(config)

    if server_config.transport == "stdio":
        logger.info("Starting astrbot-mcp over stdio")
        mcp.run(transport="stdio")
        return

    app = _asgi_app(mcp, config)
    app = GatewayMiddleware(
        app,
        auth_token=server_config.auth_token,
        health={
            "status": "ok",
            "name": server_config.name,
            "transport": server_config.transport,
            "services": config.enabled_services(),
        },
    )

    import uvicorn

    path = (
        server_config.sse_path
        if server_config.transport == "sse"
        else server_config.streamable_http_path
    )
    logger.info(
        "Starting astrbot-mcp over %s on http://%s:%s%s (auth=%s)",
        server_config.transport,
        server_config.host,
        server_config.port,
        path,
        "bearer" if server_config.auth_token else "none",
    )
    uvicorn.run(
        app,
        host=server_config.host,
        port=server_config.port,
        log_level=server_config.log_level.lower(),
    )
