from __future__ import annotations

import os
import shlex
import tempfile
from typing import Optional, Dict, Any, List

from . import execs


def build_policy_csv(username: str, namespace: str) -> str:
    role = f"role:{username}"
    ns = namespace
    lines = [
        f"p, {role}, namespaces, read, {ns}",
        f"p, {role}, database-engines, read, {ns}/*",
        f"p, {role}, database-clusters, read, {ns}/*",
        f"p, {role}, database-clusters, update, {ns}/*",
        f"p, {role}, database-clusters, create, {ns}/*",
        f"p, {role}, database-clusters, delete, {ns}/*",
        f"p, {role}, database-cluster-credentials, read, {ns}/*",
        f"g, {username}, {role}",
    ]
    return "\n".join(lines) + "\n"


def apply_rbac_policy(policy_csv: str) -> Dict[str, Any]:
    """
    Apply RBAC policy using env EVEREST_RBAC_APPLY_CMD.
    If not set, skip and return rbac_applied=False.
    If set, write policy to a temp file and run the command.
    The command may contain "{file}" placeholder; if not, append the path.
    """
    cmd_spec = os.environ.get("EVEREST_RBAC_APPLY_CMD")
    if not cmd_spec:
        return {
            "rbac_applied": False,
            "command": None,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }

    with tempfile.NamedTemporaryFile(mode="w+", suffix=".csv", delete=False) as tf:
        tf.write(policy_csv)
        tf.flush()
        policy_path = tf.name

    parts: List[str] = shlex.split(cmd_spec)
    if any("{file}" in p for p in parts):
        parts = [p.replace("{file}", policy_path) for p in parts]
    else:
        parts = parts + ["--file", policy_path]

    # Use TTY if everestctl, some subcommands may expect it
    if parts and parts[0] == "everestctl":
        res = execs.run_cmd_tty(parts)
    else:
        res = execs.run_cmd(parts)
    return {
        "rbac_applied": res.exit_code == 0,
        "command": execs.format_command(parts),
        "exit_code": res.exit_code,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "started_at": res.started_at,
        "finished_at": res.finished_at,
    }
