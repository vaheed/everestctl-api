import pytest
import httpx

from app.app import app


@pytest.mark.asyncio
async def test_set_account_password(monkeypatch):
    called = {}

    async def fake_run_cmd(cmd, **kwargs):
        # Expect set-password command
        assert cmd[:4] == ["everestctl", "accounts", "set-password", "-u"]
        called["cmd"] = cmd
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "command": " ".join(cmd)}

    from app import app as app_module
    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/accounts/password",
            json={"username": "bob", "new_password": "s3cr3t"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_update_namespace_resources(monkeypatch):
    async def fake_run_cmd(cmd, **kwargs):
        # Expect kubectl apply -n <ns> -f -
        assert cmd[:3] == ["kubectl", "apply", "-n"]
        return {"exit_code": 0, "stdout": "applied", "stderr": "", "command": " ".join(cmd)}

    from app import app as app_module
    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/namespaces/resources",
            json={"namespace": "alice", "resources": {"cpu_cores": 1, "ram_mb": 512, "disk_gb": 2}},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["applied"] is True


@pytest.mark.asyncio
async def test_update_namespace_operators_with_fallback(monkeypatch):
    calls = {"count": 0}

    async def fake_run_cmd(cmd, **kwargs):
        calls["count"] += 1
        # First call simulates unknown flag for --operator.mysql
        if calls["count"] == 1 and any("--operator.mysql" in c for c in cmd):
            return {"exit_code": 1, "stdout": "", "stderr": "unknown flag: --operator.mysql", "command": " ".join(cmd)}
        # Second call should include --operator.xtradb-cluster
        if calls["count"] == 2 and any("--operator.xtradb-cluster" in c for c in cmd):
            return {"exit_code": 0, "stdout": "ok", "stderr": "", "command": " ".join(cmd)}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "command": " ".join(cmd)}

    from app import app as app_module
    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/namespaces/operators",
            json={"namespace": "alice-ns", "operators": {"mongodb": True, "postgresql": True, "mysql": True}},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
