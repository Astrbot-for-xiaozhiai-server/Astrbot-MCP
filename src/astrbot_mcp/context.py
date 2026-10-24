"""Shared runtime context for service modules."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import AppConfig


@dataclass(slots=True)
class AppContext:
    """Holds configuration and creates short-lived HTTP clients.

    HTTP clients are intentionally created per operation. This keeps the server
    safe to run under both stdio and HTTP transports without sharing an event
    loop-bound connection pool across loops.
    """

    config: AppConfig
    started_at: float = field(default_factory=time.time)

    @property
    def security(self):
        return self.config.security

    def http_client(
        self,
        *,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        merged_headers = {"User-Agent": self.config.security.user_agent}
        if headers:
            merged_headers.update(headers)
        return httpx.AsyncClient(
            timeout=timeout
            if timeout is not None
            else self.config.security.request_timeout,
            headers=merged_headers,
            follow_redirects=False,
        )

    @staticmethod
    def path(value: str | Path) -> Path:
        return Path(value).expanduser().resolve()

    def ensure_dir(self, value: str | Path) -> Path:
        path = self.path(value)
        path.mkdir(parents=True, exist_ok=True)
        return path
