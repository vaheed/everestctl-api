import pytest
import httpx

from app.app import app


@pytest.mark.asyncio
async def test_request_id_echoes_when_provided():
    headers = {"X-Request-ID": "test-id-123"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/readyz", headers=headers)
        assert r.status_code == 200
        assert r.headers.get("X-Request-ID") == "test-id-123"


@pytest.mark.asyncio
async def test_request_id_generated_when_missing():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/healthz")
        assert r.status_code == 200
        rid = r.headers.get("X-Request-ID")
        assert rid is not None and len(rid) == 32  # uuid4 hex

