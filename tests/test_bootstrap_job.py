import asyncio
import pytest
import httpx

from app.app import app


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

    from app import app as app_module
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
