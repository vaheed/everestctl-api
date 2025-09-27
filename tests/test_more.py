import os
import asyncio
import pytest
from fastapi.testclient import TestClient

import app as appmod


@pytest.fixture()
def client():
    return TestClient(appmod.app)


def auth():
    return {"X-API-Key": os.getenv("API_KEY", "testkey")}


def test_readyz_ok(client, monkeypatch):
    async def ok_run(settings, args):
        return {"rc": 0, "stdout": {"version": "x"}, "stderr": ""}
    monkeypatch.setattr(appmod, "run_cli", ok_run)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json().get("status") == "ready"


def test_readyz_fail(client, monkeypatch):
    from fastapi import HTTPException

    async def bad_run(settings, args):
        raise HTTPException(status_code=500, detail="boom")

    monkeypatch.setattr(appmod, "run_cli", bad_run)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json().get("status") == "not_ready"


def test_metrics_toggle(client, monkeypatch):
    # Turn metrics off and ensure 404
    from app import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("METRICS_ENABLED", "false")
    r = client.get("/metrics")
    assert r.status_code == 404


def test_rate_limit_exceeded(client):
    # Force very tight limiter
    appmod.limiter.per_min = 0
    appmod.limiter.burst = 1
    appmod.limiter.state.clear()
    # First request allowed
    r1 = client.get("/healthz", headers=auth())
    assert r1.status_code == 200
    # Second immediately should be rate-limited
    r2 = client.get("/healthz", headers=auth())
    assert r2.status_code == 429


@pytest.mark.asyncio
async def test_cli_arg_validation_rejects_unsafe(monkeypatch):
    # Ensure invalid chars cause HTTPException before spawning
    from fastapi import HTTPException
    s = appmod.get_settings()
    with pytest.raises(HTTPException):
        await appmod.run_cli(s, ["do", "something;rm -rf"])  # semicolon not allowed

