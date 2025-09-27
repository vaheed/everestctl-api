import os
import time
from fastapi.testclient import TestClient

from app.app import app
from app import execs


def test_bootstrap_job_flow(monkeypatch):
    client = TestClient(app)

    def fake_run_cmd(_cmd, input_text=None, timeout=60, env=None):
        # succeed for any command
        return execs.CmdResult(0, "ok", "", 0.0, 0.0)

    monkeypatch.setattr(execs, "run_cmd", fake_run_cmd)

    r = client.post(
        "/bootstrap/users",
        headers={"X-Admin-Key": os.getenv("ADMIN_API_KEY", "changeme"), "Content-Type": "application/json"},
        json={"username": "alice"},
    )
    assert r.status_code == 202
    body = r.json()
    job_id = body["job_id"]

    # poll for completion
    for _ in range(50):
        sr = client.get(f"/jobs/{job_id}", headers={"X-Admin-Key": "changeme"})
        assert sr.status_code == 200
        status = sr.json()["status"]
        if status in ("succeeded", "failed"):
            break
        time.sleep(0.02)

    rr = client.get(f"/jobs/{job_id}/result", headers={"X-Admin-Key": "changeme"})
    assert rr.status_code == 200
    result = rr.json()
    assert result["overall_status"] in ("succeeded", "failed")
    assert isinstance(result.get("steps"), list)
