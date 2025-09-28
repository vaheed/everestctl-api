import asyncio
import pytest
import httpx

import app.app as app_module

app = app_module.app


@pytest.mark.asyncio
async def test_bootstrap_job_success(monkeypatch):
    async def fake_run_cmd(cmd, **kwargs):
        # Simulate success for all
        stdout = "ok"
        # Simulate unknown flag fallback for namespaces add with mysql
        if cmd[:3] == ["everestctl", "namespaces", "add"] and any("--operator.mysql" in c for c in cmd):
            return {"exit_code": 1, "stdout": "", "stderr": "unknown flag: --operator.mysql", "command": " ".join(cmd)}
        if cmd[:3] == ["everestctl", "namespaces", "add"] and any("--operator.xtradb-cluster" in c for c in cmd):
            return {"exit_code": 0, "stdout": stdout, "stderr": "", "command": " ".join(cmd)}
        return {"exit_code": 0, "stdout": stdout, "stderr": "", "command": " ".join(cmd)}

    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/bootstrap/users", json={"username": "alice"}, headers=headers)
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        # Poll for completion
        for _ in range(20):
            s = await ac.get(f"/jobs/{job_id}", headers=headers)
            assert s.status_code == 200
            if s.json()["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.05)

        res = await ac.get(f"/jobs/{job_id}/result", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["overall_status"] in ("succeeded",)
        # verify step names
        step_names = [s.get("name") for s in body.get("steps", [])]
        assert "create_account" in step_names
        assert "add_namespace" in step_names
        assert "apply_resource_quota" in step_names
        assert "apply_rbac_policy" in step_names


@pytest.mark.asyncio
async def test_create_account_idempotent(monkeypatch):
    async def fake_run_cmd(cmd, **kwargs):
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": "user already exists",
            "command": " ".join(cmd),
        }

    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)
    monkeypatch.setenv("BOOTSTRAP_DEFAULT_PASSWORD", "pw")

    req = app_module.BootstrapRequest(username="alice")
    outcome = await app_module._create_account(req)

    assert outcome.succeeded is True
    assert outcome.meta["account_existed"] is True
    assert "********" in outcome.result.get("command", "")


@pytest.mark.asyncio
async def test_ensure_namespace_unknown_flag(monkeypatch):
    calls = []

    async def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        if any("--operator.mysql" in c for c in cmd):
            return {"exit_code": 1, "stdout": "", "stderr": "unknown flag", "command": " ".join(cmd)}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "command": " ".join(cmd)}

    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    req = app_module.BootstrapRequest(username="bob")
    outcome = await app_module._ensure_namespace(req, "bob")

    assert outcome.succeeded is True
    assert any("--operator.xtradb-cluster" in c for cmd in calls for c in cmd)
