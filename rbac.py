import datetime as dt
import fcntl
import logging
import os
import shutil
import tempfile
from typing import Iterable, List, Tuple


logger = logging.getLogger(__name__)


def _lock_file(path: str):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    f = os.fdopen(fd, "r+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def append_policy_lines(policy_file: str, lines: Iterable[str], rotate_backup: bool = True) -> Tuple[str, int]:
    os.makedirs(os.path.dirname(policy_file), exist_ok=True)
    lock_path = policy_file + ".lock"
    with _lock_file(lock_path) as lf:
        # Read existing
        current = ""
        if os.path.exists(policy_file):
            with open(policy_file, "r", encoding="utf-8") as rf:
                current = rf.read()
        content = current.rstrip("\n") + "\n" + "\n".join([l.rstrip("\n") for l in lines]) + "\n"
        # Backup
        backup_path = None
        if rotate_backup and os.path.exists(policy_file):
            ts = dt.datetime.utcnow().isoformat(timespec="seconds").replace(":", "-")
            backup_path = policy_file + f".bak.{ts}"
            shutil.copy2(policy_file, backup_path)
        # Write temp
        dirn = os.path.dirname(policy_file)
        fd, tmp_path = tempfile.mkstemp(prefix="policy.csv.tmp.", dir=dirn)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as wf:
                wf.write(content)
                wf.flush()
                os.fsync(wf.fileno())
            os.replace(tmp_path, policy_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            # Attempt rollback from backup
            if backup_path:
                shutil.copy2(backup_path, policy_file)
            raise
    return policy_file, len(lines)


def validate_policy(run_cli, policy_file: str) -> Tuple[int, str, str]:
    argv = ["everestctl", "settings", "rbac", "validate", "--policy-file", policy_file]
    code, out, err = run_cli(argv)
    return code, out, err


def can_check(run_cli, policy_file: str, user: str, resource: str, verb: str, obj: str) -> Tuple[int, str, str]:
    argv = [
        "everestctl",
        "settings",
        "rbac",
        "can",
        "--policy-file",
        policy_file,
        "--user",
        user,
        "--resource",
        resource,
        "--verb",
        verb,
        "--object",
        obj,
    ]
    code, out, err = run_cli(argv)
    return code, out, err


def validate_policy_lines(lines: List[str]) -> None:
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split(",")]
        if len(parts) < 2:
            raise ValueError(f"invalid rbac line {i}: '{line}'")
        if parts[0] not in ("p", "g"):
            raise ValueError(f"invalid rbac prefix on line {i}: '{parts[0]}'")

