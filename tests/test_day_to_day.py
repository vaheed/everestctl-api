import asyncio

import httpx
import pytest

from app.app import app


async def _wait_for_job(ac: httpx.AsyncClient, job_id: str, headers: dict) -> dict:
    for _ in range(40):
        status = await ac.get(f"/jobs/{job_id}", headers=headers)
        assert status.status_code == 200
        body = status.json()
        if body["status"] in {"succeeded", "failed"}:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish in time")


async def _fetch_job_result(ac: httpx.AsyncClient, job_id: str, headers: dict) -> dict:
    res = await ac.get(f"/jobs/{job_id}/result", headers=headers)
    assert res.status_code == 200
    return res.json()


@pytest.mark.asyncio
async def test_set_account_password(monkeypatch):
    async def fake_run_cmd(cmd, **kwargs):
        assert cmd[:4] == ["everestctl", "accounts", "set-password", "-u"]
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
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        await _wait_for_job(ac, job_id, headers)
        body = await _fetch_job_result(ac, job_id, headers)

        assert body["ok"] is True
        step_names = [s.get("name") for s in body.get("steps", [])]
        assert "set_password_stdin" in step_names


@pytest.mark.asyncio
async def test_update_namespace_resources(monkeypatch):
    async def fake_run_cmd(cmd, **kwargs):
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
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        await _wait_for_job(ac, job_id, headers)
        body = await _fetch_job_result(ac, job_id, headers)

        assert body["applied"] is True
        assert body["ok"] is True


@pytest.mark.asyncio
async def test_update_namespace_operators_with_fallback(monkeypatch):
    calls = {"count": 0}

    async def fake_run_cmd(cmd, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1 and any("--operator.mysql" in c for c in cmd):
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": "unknown flag: --operator.mysql",
                "command": " ".join(cmd),
            }
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
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        await _wait_for_job(ac, job_id, headers)
        body = await _fetch_job_result(ac, job_id, headers)

        assert body["ok"] is True
        assert body["namespace"] == "alice-ns"
        step_names = [s.get("name") for s in body.get("steps", [])]
        assert "update_namespace_operators" in step_names
