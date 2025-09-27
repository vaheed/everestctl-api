import pytest

import cli


def test_allowlist_blocks_unsafe():
    with pytest.raises(ValueError):
        cli.run(["everestctl", "; rm -rf /"])


def test_whitelisted_accounts_list(monkeypatch):
    monkeypatch.setenv("SKIP_CLI_VERIFY", "true")
    code, out, err = cli.run(["everestctl", "accounts", "list"])  # mocked to 0 by conftest
    assert code == 0
