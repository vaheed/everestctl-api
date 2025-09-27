import os
import json
import tempfile
from typing import Dict, Any

from .execs import run_cmd


def build_policy_csv(username: str, namespace: str) -> str:
    lines = [
        f"p, role:{username}, namespaces, read, {namespace}",
        # engines must be readable across all to enable DB creation
        f"p, role:{username}, database-engines, read, */*",
        f"p, role:{username}, database-clusters, *, {namespace}/*",
        f"p, role:{username}, database-cluster-backups, *, {namespace}/*",
        f"p, role:{username}, database-cluster-restores, *, {namespace}/*",
        f"p, role:{username}, database-cluster-credentials, read, {namespace}/*",
        f"p, role:{username}, backup-storages, *, {namespace}/*",
        f"p, role:{username}, monitoring-instances, *, {namespace}/*",
        f"g, {username}, role:{username}",
    ]
    return "\n".join(lines) + "\n"


async def apply_policy_if_configured(username: str, namespace: str, timeout: int = 60) -> Dict[str, Any]:
    """
    If EVEREST_RBAC_APPLY_CMD is set, write a temp policy.csv and apply.
    The command may include {file} placeholder.
    Returns a step-like dict with rbac_applied flag.
    """
    cmd_tpl = os.environ.get("EVEREST_RBAC_APPLY_CMD")
    if not cmd_tpl:
        return {
            "name": "apply_rbac_policy",
            "command": "<skipped>",
            "exit_code": 0,
            "rbac_applied": False,
            "stdout": "",
            "stderr": "RBAC command not configured; skipping",
        }

    policy = build_policy_csv(username, namespace)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="policy_", suffix=".csv", delete=False) as tf:
            tf.write(policy.encode())
            temp_path = tf.name

        # Replace placeholder
        cmd_str = cmd_tpl.replace("{file}", temp_path)
        cmd = cmd_str.split()
        res = await run_cmd(cmd, timeout=timeout)
        res.update({
            "name": "apply_rbac_policy",
            "rbac_applied": res.get("exit_code", 1) == 0,
        })
        return res
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass


async def revoke_user_in_rbac_configmap(username: str, timeout: int = 60) -> Dict[str, Any]:
    """
    Remove policy and bindings for a user from the everest RBAC ConfigMap.
    This directly patches the ConfigMap in everest-system namespace.
    Returns a step-like dict with rbac_changed flag.
    """
    get_res = await run_cmd([
        "kubectl",
        "-n",
        "everest-system",
        "get",
        "configmap",
        "everest-rbac",
        "-o",
        "json",
    ], timeout=timeout)
    step = {"name": "revoke_rbac_user", "command": get_res.get("command", ""), "exit_code": get_res.get("exit_code", 1)}
    if get_res.get("exit_code") != 0:
        step.update({"rbac_changed": False, "stdout": get_res.get("stdout", ""), "stderr": get_res.get("stderr", "")})
        return step

    try:
        obj = json.loads(get_res.get("stdout", "{}"))
        data = obj.get("data", {})
        enabled_val = data.get("enabled", "true")
        policy = data.get("policy.csv", "")
        lines = [ln for ln in policy.splitlines()]
        new_lines = []
        user_role = f"role:{username}"
        for ln in lines:
            s = ln.strip()
            if not s:
                new_lines.append(ln)
                continue
            # Remove user binding and the role's policies
            if s.startswith(f"g, {username}, "):
                continue
            if s.startswith("p, ") and (f", {user_role}," in s or s.startswith(f"p, {user_role},")):
                continue
            new_lines.append(ln)
        if new_lines == lines:
            # Nothing to change
            return {
                "name": "revoke_rbac_user",
                "command": get_res.get("command", ""),
                "exit_code": 0,
                "rbac_changed": False,
                "stdout": "no changes needed",
                "stderr": "",
            }
        # Build YAML for apply
        new_policy = "\n".join(new_lines) + ("\n" if new_lines and not new_lines[-1].endswith("\n") else "")
        indented_policy = "".join([("    " + ln + ("\n" if not ln.endswith("\n") else "")) for ln in new_policy.splitlines()])
        manifest = f"""
apiVersion: v1
kind: ConfigMap
metadata:
  name: everest-rbac
  namespace: everest-system
data:
  enabled: "{enabled_val}"
  policy.csv: |-
{indented_policy}
""".strip() + "\n"
    except Exception as e:
        return {"name": "revoke_rbac_user", "exit_code": 1, "rbac_changed": False, "stdout": "", "stderr": f"parse error: {e}"}

    apply_res = await run_cmd([
        "kubectl",
        "apply",
        "-f",
        "-",
    ], input_text=manifest, timeout=timeout)
    apply_res.update({
        "name": "revoke_rbac_user",
        "rbac_changed": apply_res.get("exit_code", 1) == 0,
        "manifest_preview": manifest[:5000],
    })
    return apply_res
