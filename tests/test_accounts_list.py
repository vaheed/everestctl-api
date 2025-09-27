import json
from fastapi.testclient import TestClient

from app.app import app
from app import execs


ADMIN_KEY = "testkey"


def setup_module():
    # Override admin key for tests
    app.dependency_overrides = {}


def auth_headers():
    return {"X-Admin-Key": app.__dict__.get("ADMIN_API_KEY", "changeme") or ADMIN_KEY}


def test_accounts_list_json(monkeypatch):
    client = TestClient(app)

    def fake_run_cmd(_cmd, input_text=None, timeout=60, env=None):
        return execs.CmdResult(0, json.dumps({"ok": True}), "", 0.0, 0.0)

    monkeypatch.setattr(execs, "run_cmd", fake_run_cmd)
    r = client.get("/accounts/list", headers={"X-Admin-Key": "changeme"})
    assert r.status_code == 200
    assert r.json() == {"data": {"ok": True}}


def test_accounts_list_table(monkeypatch):
    client = TestClient(app)

    def fake_run_cmd(_cmd, input_text=None, timeout=60, env=None):
        table = "NAME    ID\nalice   1\n"
        return execs.CmdResult(0, table, "", 0.0, 0.0)

    monkeypatch.setattr(execs, "run_cmd", fake_run_cmd)
    r = client.get("/accounts/list", headers={"X-Admin-Key": "changeme"})
    assert r.status_code == 200
    assert r.json() == {"data": [{"NAME": "alice", "ID": "1"}]}


def test_accounts_list_error(monkeypatch):
    client = TestClient(app)

    def fake_run_cmd(_cmd, input_text=None, timeout=60, env=None):
        return execs.CmdResult(1, "", "boom", 0.0, 0.0)

    monkeypatch.setattr(execs, "run_cmd", fake_run_cmd)
    r = client.get("/accounts/list", headers={"X-Admin-Key": "changeme"})
    assert r.status_code == 502
    body = r.json()
    assert body["detail"]["error"] == "everestctl failed"

