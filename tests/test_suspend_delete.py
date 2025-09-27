import json
import pytest
import httpx

from app.app import app


@pytest.mark.asyncio
async def test_suspend_user_flow(monkeypatch):
    # Simulate: no deactivate available (all variants fail), scale succeeds, RBAC revoke succeeds
    calls = {"i": 0}

    async def fake_run_cmd(cmd, **kwargs):
        calls["i"] += 1
        c = " ".join(cmd)
        if cmd[:3] == ["everestctl", "accounts", "deactivate"]:
            return {"exit_code": 1, "stdout": "", "stderr": "unknown command", "command": c}
        if cmd[:3] == ["everestctl", "accounts", "disable"]:
            return {"exit_code": 1, "stdout": "", "stderr": "unknown command", "command": c}
        if cmd[:3] == ["everestctl", "accounts", "suspend"]:
            return {"exit_code": 1, "stdout": "", "stderr": "unknown command", "command": c}
        if cmd[:3] == ["everestctl", "accounts", "lock"]:
            return {"exit_code": 1, "stdout": "", "stderr": "unknown command", "command": c}
        if cmd[:3] == ["kubectl", "scale", "statefulset"]:
            return {"exit_code": 0, "stdout": "scaled", "stderr": "", "command": c}
        if cmd[:7] == ["kubectl", "-n", "everest-system", "get", "configmap", "everest-rbac", "-o"]:
            payload = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "data": {
                    "enabled": "true",
                    "policy.csv": "\n".join([
                        "p, role:alice, namespaces, read, alice",
                        "g, alice, role:alice",
                        "p, role:bob, namespaces, read, team-bob",
                        "g, bob, role:bob",
                    ])
                }
            }
            return {"exit_code": 0, "stdout": json.dumps(payload), "stderr": "", "command": c}
        if cmd[:2] == ["kubectl", "apply"]:
            return {"exit_code": 0, "stdout": "configmap/everest-rbac configured", "stderr": "", "command": c}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "command": c}

    from app import app as app_module
    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/accounts/suspend",
            json={"username": "bob", "namespace": "team-bob"},
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        names = [s.get("name") for s in body.get("steps", [])]
        assert "scale_down_statefulsets" in names
        assert "revoke_rbac_user" in names


@pytest.mark.asyncio
async def test_delete_user_flow_with_fallbacks(monkeypatch):
    # Simulate: everestctl namespaces remove fails then kubectl delete succeeds; accounts delete succeeds
    async def fake_run_cmd(cmd, **kwargs):
        c = " ".join(cmd)
        if cmd[:3] == ["everestctl", "namespaces", "remove"]:
            return {"exit_code": 1, "stdout": "", "stderr": "not found", "command": c}
        if cmd[:3] == ["kubectl", "delete", "namespace"]:
            return {"exit_code": 0, "stdout": "namespace deleted", "stderr": "", "command": c}
        if cmd[:7] == ["kubectl", "-n", "everest-system", "get", "configmap", "everest-rbac", "-o"]:
            # minimal CM with bob policy/binding
            payload = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "data": {
                    "enabled": "true",
                    "policy.csv": "\n".join(["p, role:bob, namespaces, read, team-bob", "g, bob, role:bob"])},
            }
            return {"exit_code": 0, "stdout": json.dumps(payload), "stderr": "", "command": c}
        if cmd[:2] == ["kubectl", "apply"]:
            return {"exit_code": 0, "stdout": "cm applied", "stderr": "", "command": c}
        if cmd[:3] == ["everestctl", "accounts", "delete"]:
            return {"exit_code": 0, "stdout": "deleted", "stderr": "", "command": c}
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "command": c}

    from app import app as app_module
    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/accounts/delete",
            json={"username": "bob", "namespace": "team-bob"},
            headers=headers,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        step_names = [s.get("name") for s in body.get("steps", [])]
        assert "delete_namespace" in step_names or "remove_namespace" in step_names
        assert "delete_account" in step_names

