from fastapi.testclient import TestClient


def test_bootstrap(client: TestClient):
    resp = client.post(
        "/bootstrap/tenant",
        headers={"X-Admin-Key": "test-key"},
        json={
            "username": "alice",
            "password": "StrongP@ssw0rd",
            "namespace": "ns-alice",
            "operators": {"postgresql": True, "mongodb": False, "xtradb_cluster": True},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "alice"
