"""Service modules. Each exposes ``register(mcp, ctx)`` and checks its own
``enabled`` flag before registering tools."""

__all__ = [
    "http_api",
    "web_search",
    "public_apis",
    "files",
    "sqlite_db",
    "github",
    "mail",
    "calendar",
    "knowledge",
]
