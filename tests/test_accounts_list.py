import pytest
import httpx

from app.app import app


@pytest.mark.asyncio
async def test_accounts_list_json(monkeypatch):
    async def fake_run_cmd(cmd, **kwargs):
        if "--json" in cmd:
            return {"exit_code": 0, "stdout": '{"items":[{"name":"alice"}]}', "stderr": "", "command": " ".join(cmd)}
        return {"exit_code": 1, "stdout": "", "stderr": "", "command": " ".join(cmd)}

    from app import app as app_module
    monkeypatch.setattr(app_module, "run_cmd", fake_run_cmd)

    headers = {"X-Admin-Key": "changeme"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/accounts/list", headers=headers)
        assert r.status_code == 200
        assert r.json()["data"]["items"][0]["name"] == "alice"
