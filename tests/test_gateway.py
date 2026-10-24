import httpx
import pytest

from astrbot_mcp.server import GatewayMiddleware


async def dummy_app(scope, receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.asyncio
async def test_health_and_bearer_auth():
    app = GatewayMiddleware(dummy_app, auth_token="secret", health={"status": "ok"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        unauthorized = await client.post("/mcp", json={})
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"].startswith("Bearer")

        authorized = await client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer secret"}
        )
        assert authorized.status_code == 200
        assert authorized.text == "ok"


@pytest.mark.asyncio
async def test_no_auth_passes_through():
    app = GatewayMiddleware(dummy_app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/mcp", json={})
        assert response.status_code == 200
