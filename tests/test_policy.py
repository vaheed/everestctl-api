import os
from app import rbac_add, rbac_remove


def test_policy_backup_created(tmp_path):
    path = tmp_path / "policy.csv"
    # initial add creates the file (no backup yet)
    rbac_add(str(path), "alice", "ns1")
    assert path.exists()
    # removing will rewrite atomically and should create a backup of existing file
    rbac_remove(str(path), "alice", "ns1")
    backups = list(tmp_path.glob("policy.csv.*.bak"))
    # At least one backup file should exist
    assert len(backups) >= 1

