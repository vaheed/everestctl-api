import json, pathlib, os
import pytest
from fastapi.testclient import TestClient
from app import app, get_settings, Settings, rbac_add, rbac_remove, read_policy, write_policy_atomic, init_db, get_counter, inc_counter

@pytest.fixture(autouse=True)
def test_env(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("data")
    policy = d / "policy.csv"
    db = d / "audit.db"
    monkeypatch.setenv("API_KEY","testkey")
    monkeypatch.setenv("RBAC_POLICY_PATH", str(policy))
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("EVERESTCTL_PATH", "/bin/true")  # simulate presence
    monkeypatch.setenv("SKIP_CLI_CHECK", "1")          # skip CLI presence on non-Linux
    # Refresh settings cache after env changes
    get_settings.cache_clear()
    init_db(str(db))
    yield

@pytest.fixture()
def client():
    return TestClient(app)

def auth():
    return {"X-API-Key":"testkey"}

def test_rbac_append_remove(tmp_path):
    path = tmp_path / "policy.csv"
    rbac_add(str(path), "alice", "ns1")
    rows = read_policy(str(path))
    assert ["p","alice","ns1","write"] in rows
    rbac_remove(str(path), "alice", "ns1")
    rows2 = read_policy(str(path))
    assert ["p","alice","ns1","write"] not in rows2

def test_quota_counters(tmp_path):
    db = tmp_path / "a.db"
    os.environ["DB_PATH"] = str(db)
    init_db(str(db))
    assert get_counter(str(db),"ten1")==0
    assert inc_counter(str(db),"ten1",+1)==1
    assert inc_counter(str(db),"ten1",+3)==4
    assert inc_counter(str(db),"ten1",-10)==0

def test_health(client):
    r = client.get("/healthz")
    assert r.status_code==200 and r.text=="ok"

def test_auth_required(client):
    r = client.get("/tenants/alice/quota")
    assert r.status_code==401

def test_quota_endpoint(client, monkeypatch):
    # set counter directly
    s = get_settings()
    # ensure DB exists
    init_db(s.DB_PATH)
    from app import inc_counter
    inc_counter(s.DB_PATH, "alice", 2)
    r = client.get("/tenants/alice/quota", headers=auth())
    assert r.status_code==200
    assert r.json()["used_clusters"]==2

def test_create_tenant_cli_mock(client, monkeypatch):
    async def fake_run(settings, args):
        return {"rc":0,"stdout":{"ok":True},"stderr":""}
    monkeypatch.setattr("app.run_cli", fake_run)
    body = {"user":"alice","namespace":"ns1","password":"p","engine":"postgres"}
    r = client.post("/tenants/create", headers=auth(), json=body)
    assert r.status_code==200
    assert r.json()["status"]=="created"
    # Verify audit counter incremented
    s = get_settings()
    assert get_counter(s.DB_PATH, "alice") == 1

def test_delete_tenant_cli_mock(client, monkeypatch):
    # seed counter
    s = get_settings()
    init_db(s.DB_PATH)
    inc_counter(s.DB_PATH, "alice", +1)
    async def fake_run(settings, args):
        return {"rc":0,"stdout":{"ok":True},"stderr":""}
    monkeypatch.setattr("app.run_cli", fake_run)
    body = {"user":"alice","namespace":"ns1"}
    r = client.post("/tenants/delete", headers=auth(), json=body)
    assert r.status_code==200
    assert r.json()["status"]=="deleted"
    assert get_counter(s.DB_PATH, "alice") == 0

def test_rotate_password_cli_mock(client, monkeypatch):
    async def fake_run(settings, args):
        return {"rc":0,"stdout":{"ok":True},"stderr":""}
    monkeypatch.setattr("app.run_cli", fake_run)
    r = client.post("/tenants/rotate-password", headers=auth(), json={"user":"alice","new_password":"n"})
    assert r.status_code==200
    assert r.json()["status"]=="rotated"

def test_engine_not_allowed(client, monkeypatch):
    async def fake_run(settings, args):
        return {"rc":0,"stdout":{"ok":True},"stderr":""}
    monkeypatch.setattr("app.run_cli", fake_run)
    body = {"user":"bob","namespace":"ns2","password":"p","engine":"oracle"}
    r = client.post("/tenants/create", headers=auth(), json=body)
    assert r.status_code==400
