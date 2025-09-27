import os
import pytest
from fastapi.testclient import TestClient
import app as appmod


@pytest.fixture(autouse=True)
def sso_env(monkeypatch, tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    monkeypatch.setenv("API_KEY", "testkey")
    monkeypatch.setenv("RBAC_POLICY_PATH", str(d / "policy.csv"))
    monkeypatch.setenv("DB_PATH", str(d / "audit.db"))
    monkeypatch.setenv("EVERESTCTL_PATH", "/bin/true")
    monkeypatch.setenv("SKIP_CLI_CHECK", "1")
    monkeypatch.setenv("SSO_ENABLED", "true")
    appmod.get_settings.cache_clear()
    yield


def auth():
    return {"X-API-Key": "testkey"}


def test_rotate_password_disabled(monkeypatch):
    client = TestClient(appmod.app)
    r = client.post("/tenants/rotate-password", headers=auth(), json={"user":"a","new_password":"x"})
    assert r.status_code == 400
    assert "disabled" in r.json().get("error", "") or r.json().get("error") == "password rotation is disabled when SSO is enabled"


def test_create_tenant_skips_local_user(monkeypatch):
    # When SSO is enabled, user create and set-password should not be called
    called = []
    async def fake_run(settings, args):
        called.append(args)
        return {"rc": 0, "stdout": {"ok": True}, "stderr": ""}
    monkeypatch.setattr(appmod, "run_cli", fake_run)
    client = TestClient(appmod.app)
    body = {"user":"alice","namespace":"ns1","password":"p","engine":"postgres"}
    r = client.post("/tenants/create", headers=auth(), json=body)
    assert r.status_code == 200
    # Ensure no accounts set-password call when SSO is enabled
    assert not any(x[:2] == ["accounts", "set-password"] for x in called)
