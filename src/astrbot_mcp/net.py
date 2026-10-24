"""SSRF-aware HTTP helpers.

Every user-supplied URL is validated before use *and* on each redirect hop.
Private, loopback, link-local, multicast and reserved addresses are rejected
unless ``security.allow_private_networks`` (or a per-call override) is enabled.

Residual risk: DNS is resolved twice (validation and connection), so a
malicious DNS server could rebind between the two lookups. Run the server in a
network namespace / egress-restricted environment if that matters to you.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from .config import SecurityConfig
from .context import AppContext
from .utils import decode_bytes


class UrlNotAllowed(ValueError):
    """Raised when a URL fails the SSRF / allow-list checks."""


@dataclass(slots=True)
class SafeResponse:
    status: int
    headers: dict[str, str]
    content: bytes
    url: str
    truncated: bool

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return decode_bytes(self.content, self.content_type)


def _match_domain(host: str, pattern: str) -> bool:
    host = host.lower().rstrip(".")
    pattern = pattern.lower().strip().lstrip(".").rstrip(".")
    if not pattern:
        return False
    return host == pattern or host.endswith("." + pattern)


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        not ip.is_global
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _resolve_host(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UrlNotAllowed(f"Cannot resolve host {host!r}: {exc}") from exc

    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    if not addresses:
        raise UrlNotAllowed(f"Cannot resolve host {host!r}")
    return addresses


async def validate_url(
    url: str,
    security: SecurityConfig,
    *,
    allow_private: bool | None = None,
) -> None:
    """Raise :class:`UrlNotAllowed` when ``url`` must not be requested."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise UrlNotAllowed("Only http:// and https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise UrlNotAllowed("URLs containing credentials are not allowed")

    host = parsed.hostname
    if not host:
        raise UrlNotAllowed("URL is missing a hostname")
    host = host.strip()

    if security.allowed_domains and not any(
        _match_domain(host, pattern) for pattern in security.allowed_domains
    ):
        raise UrlNotAllowed(f"Host {host!r} is not in security.allowed_domains")

    for pattern in security.blocked_domains:
        if _match_domain(host, pattern):
            raise UrlNotAllowed(f"Host {host!r} is blocked by security.blocked_domains")

    effective_allow_private = (
        security.allow_private_networks if allow_private is None else allow_private
    )
    if effective_allow_private:
        return

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _blocked_ip(literal):
            raise UrlNotAllowed(
                f"Refusing to connect to private/reserved address {host}"
            )
        return

    for address in await _resolve_host(host):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _blocked_ip(ip):
            raise UrlNotAllowed(
                f"Host {host!r} resolves to a private/reserved address ({address}). "
                "Set security.allow_private_networks=true to allow this."
            )


async def safe_request(
    ctx: AppContext,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    content: str | bytes | None = None,
    data: dict[str, Any] | None = None,
    timeout: float | None = None,
    max_bytes: int | None = None,
    allow_private: bool | None = None,
    follow_redirects: bool = False,
) -> SafeResponse:
    """Perform an HTTP request with SSRF checks, redirect re-validation and a
    hard response-size limit."""
    security = ctx.config.security
    limit = max_bytes if max_bytes is not None else security.max_response_bytes
    max_redirects = security.max_redirects

    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ValueError(f"Unsupported HTTP method: {method}")

    current_url = url
    current_method = method
    current_params = params
    current_json = json_body
    current_content = content
    current_data = data

    async with ctx.http_client(timeout=timeout, headers=headers) as client:
        for redirect_count in range(max_redirects + 1):
            await validate_url(current_url, security, allow_private=allow_private)

            async with client.stream(
                current_method,
                current_url,
                params=current_params,
                json=current_json,
                content=current_content,
                data=current_data,
            ) as response:
                location = response.headers.get("location")
                if (
                    follow_redirects
                    and response.status_code in {301, 302, 303, 307, 308}
                    and location
                ):
                    if redirect_count >= max_redirects:
                        raise UrlNotAllowed(
                            f"Too many redirects (>{max_redirects}) requesting {url}"
                        )
                    current_url = urljoin(current_url, location)
                    if response.status_code == 303 or (
                        response.status_code in {301, 302} and current_method == "POST"
                    ):
                        current_method = "GET"
                        current_json = None
                        current_content = None
                        current_data = None
                    current_params = None
                    continue

                chunks: list[bytes] = []
                total = 0
                truncated = False
                if current_method != "HEAD":
                    async for chunk in response.aiter_bytes():
                        remaining = limit - total
                        if remaining <= 0:
                            truncated = True
                            break
                        if len(chunk) > remaining:
                            chunks.append(chunk[:remaining])
                            total += remaining
                            truncated = True
                            break
                        chunks.append(chunk)
                        total += len(chunk)

                body = b"".join(chunks)
                return SafeResponse(
                    status=response.status_code,
                    headers={
                        key.lower(): value for key, value in response.headers.items()
                    },
                    content=body,
                    url=str(response.url),
                    truncated=truncated,
                )

    raise UrlNotAllowed(f"Request to {url} did not produce a response")
