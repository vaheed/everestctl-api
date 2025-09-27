import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.app as app_module


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "secret123")
    # Ensure availability unless a test overrides it
    monkeypatch.setattr(app_module, "EVERESTCTL_AVAILABLE", True, raising=False)


def get_client():
    return TestClient(app_module.app)


def test_unauthorized_missing_header():
    client = get_client()
    r = client.get("/accounts/list")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_unauthorized_wrong_header():
    client = get_client()
    r = client.get("/accounts/list", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_json_output(monkeypatch):
    # Mock subprocess.run to return JSON output
    payload = [{"id": "1", "name": "Alice"}]

    def mock_run(*args, **kwargs):
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = get_client()
    r = client.get("/accounts/list", headers={"X-Admin-Key": "secret123"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "everestctl account list"
    assert body["data"] == payload


def test_text_output_pipes(monkeypatch):
    text = """ID | NAME\n1 | Alice\n2 | Bob\n"""

    def mock_run(*args, **kwargs):
        return SimpleNamespace(stdout=text, stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = get_client()
    r = client.get("/accounts/list", headers={"X-Admin-Key": "secret123"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0].get("id") == "1"
    assert data[0].get("name") == "Alice"


def test_everestctl_error(monkeypatch):
    def mock_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], output="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", mock_run)
    client = get_client()
    r = client.get("/accounts/list", headers={"X-Admin-Key": "secret123"})
    assert r.status_code == 502
    assert r.json()["error"] == "everestctl failed"
    assert "boom" in r.json()["detail"]


def test_everestctl_not_available(monkeypatch):
    monkeypatch.setattr(app_module, "EVERESTCTL_AVAILABLE", False, raising=False)
    client = get_client()
    r = client.get("/accounts/list", headers={"X-Admin-Key": "secret123"})
    assert r.status_code == 500
    assert r.json()["error"] == "everestctl not found"

