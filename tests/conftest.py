"""Shared pytest fixtures: offline config and mocked HTTP transports."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from astrbot_mcp.config import AppConfig
from astrbot_mcp.context import AppContext

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


@pytest.fixture
def config_factory(tmp_path: Path) -> Callable[..., AppConfig]:
    """Build an :class:`AppConfig` with only the requested services enabled."""

    def make(**enabled: dict[str, Any] | None) -> AppConfig:
        config = AppConfig()
        for name in ALL_SERVICES:
            getattr(config.services, name).enabled = False

        config.services.files.root = str(tmp_path / "files")
        config.services.sqlite.root = str(tmp_path / "sqlite")
        config.services.calendar.root = str(tmp_path / "calendar")
        config.services.knowledge.db_path = str(tmp_path / "knowledge.db")

        for name, overrides in enabled.items():
            service = getattr(config.services, name)
            service.enabled = True
            for key, value in (overrides or {}).items():
                setattr(service, key, value)
        return config

    return make


@pytest.fixture
def payload() -> Callable[[Any], dict[str, Any]]:
    """Decode the JSON returned by an MCP tool into a plain dictionary."""

    def parse(result: Any) -> dict[str, Any]:
        # FastMCP may return (content_blocks, structured_content).
        if isinstance(result, tuple) and len(result) == 2:
            content, structured = result
            if isinstance(structured, dict) and "result" in structured:
                structured = structured["result"]
                return (
                    json.loads(structured)
                    if isinstance(structured, str)
                    else structured
                )
            result = content
        if isinstance(result, dict):
            return result
        for item in result:
            text = getattr(item, "text", None)
            if text:
                return json.loads(text)
        raise AssertionError(f"Unexpected tool result: {result!r}")

    return parse


@pytest.fixture
def patch_http(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Route every ``AppContext.http_client()`` call through a mock transport."""

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        def fake_http_client(
            self: AppContext,
            *,
            timeout: float | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.AsyncClient:
            merged = {"User-Agent": self.config.security.user_agent}
            if headers:
                merged.update(headers)
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                headers=merged,
            )

        monkeypatch.setattr(AppContext, "http_client", fake_http_client)

    return install
