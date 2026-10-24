import pytest

from astrbot_mcp.config import SecurityConfig
from astrbot_mcp.net import UrlNotAllowed, validate_url


@pytest.mark.asyncio
async def test_private_addresses_blocked():
    security = SecurityConfig()
    for url in (
        "http://127.0.0.1/admin",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
    ):
        with pytest.raises(UrlNotAllowed):
            await validate_url(url, security)


@pytest.mark.asyncio
async def test_allow_private_override():
    security = SecurityConfig()
    await validate_url("http://127.0.0.1:8080/health", security, allow_private=True)


@pytest.mark.asyncio
async def test_allowlist_and_blocklist():
    security = SecurityConfig(allowed_domains=["example.com"])
    with pytest.raises(UrlNotAllowed):
        await validate_url("https://evil.test/", security)

    security = SecurityConfig(blocked_domains=["bad.example"])
    with pytest.raises(UrlNotAllowed):
        await validate_url("https://api.bad.example/x", security)
