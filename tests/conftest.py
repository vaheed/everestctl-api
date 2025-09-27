import os
import sys
import types
import pytest
from fastapi.testclient import TestClient

# Ensure repository root is importable (so 'import cli', 'import app', etc.)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cli as cli_module


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SKIP_CLI_VERIFY", "true")
    monkeypatch.setenv("ADMIN_API_KEY", "test-key")
    monkeypatch.setenv("POLICY_FILE", "/tmp/policy.csv")
    monkeypatch.setenv("SQLITE_DB", "/tmp/tenant_proxy_test.db")
    yield


@pytest.fixture(autouse=True)
def stub_cli(monkeypatch):
    def fake_run(argv, timeout=None, retries=None, stdin_input=None):
        cmd = " ".join(argv)
        if argv[:3] == ["everestctl", "accounts", "list"]:
            return 0, "alice\n", ""
        if argv[:3] == ["everestctl", "namespaces", "list"]:
            return 0, "ns-alice\n", ""
        return 0, "ok", ""

    monkeypatch.setattr(cli_module, "run", fake_run)
    monkeypatch.setattr(cli_module, "accounts_create", lambda u: (0, "", ""))
    monkeypatch.setattr(cli_module, "accounts_set_password", lambda u, p=None: (0, "", ""))
    monkeypatch.setattr(cli_module, "accounts_list", lambda: (0, "alice\n", ""))
    monkeypatch.setattr(cli_module, "accounts_delete", lambda u: (0, "", ""))
    monkeypatch.setattr(cli_module, "namespaces_add", lambda n, ops, take_ownership=False: (0, "", ""))
    monkeypatch.setattr(cli_module, "namespaces_update", lambda n: (0, "", ""))
    monkeypatch.setattr(cli_module, "namespaces_remove", lambda n, keep_namespace=False: (0, "", ""))
    monkeypatch.setattr(cli_module, "rbac_validate", lambda pf=None: (0, "", ""))
    monkeypatch.setattr(cli_module, "rbac_can", lambda pf, u, r, v, o: (0, "allow", ""))
    monkeypatch.setattr(cli_module, "get_version", lambda: (0, "v0.0.0", ""))
    monkeypatch.setattr(cli_module, "verify_commands", lambda: True)
    yield


@pytest.fixture
def client():
    # Recreate app after monkeypatch to pick env
    import importlib
    import app as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app)
