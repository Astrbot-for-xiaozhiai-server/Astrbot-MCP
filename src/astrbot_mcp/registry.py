"""Service registry."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from .context import AppContext
from .services import (
    calendar,
    files,
    github,
    http_api,
    knowledge,
    mail,
    public_apis,
    sqlite_db,
    web_search,
)

Registrar = Callable[[FastMCP, AppContext], None]

SERVICE_MODULES: tuple[tuple[str, Registrar], ...] = (
    ("http_api", http_api.register),
    ("web_search", web_search.register),
    ("public_apis", public_apis.register),
    ("files", files.register),
    ("sqlite", sqlite_db.register),
    ("github", github.register),
    ("mail", mail.register),
    ("calendar", calendar.register),
    ("knowledge", knowledge.register),
)


def register_all(mcp: FastMCP, ctx: AppContext) -> list[str]:
    """Register every enabled service and return the registered names."""
    registered: list[str] = []
    for name, registrar in SERVICE_MODULES:
        service_config = getattr(ctx.config.services, name, None)
        if service_config is None or not getattr(service_config, "enabled", False):
            continue
        registrar(mcp, ctx)
        registered.append(name)
    return registered
