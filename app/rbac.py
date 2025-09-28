import os
import json
import shlex
import tempfile
from typing import Dict, Any

from .execs import run_cmd


def build_policy_csv(username: str, namespace: str) -> str:
    lines = [
        f"p, role:{username}, namespaces, *, {namespace}",
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


def _ensure_admin_baseline(existing_policy: str) -> str:
    """
    Ensure the policy contains a functional admin baseline so enabling
    ConfigMap-based RBAC doesn't lock out administrators.

    - Guarantees the group binding: g, admin, role:admin
    - Adds permissive admin rules if none exist
    """
    lines = [ln for ln in existing_policy.splitlines() if ln is not None]
    # Flags
    has_admin_group = any(ln.strip().startswith("g, admin, role:admin") for ln in lines)
    has_admin_policies = any(ln.strip().startswith("p, role:admin,") for ln in lines)

    if not has_admin_group:
        lines.insert(0, "g, admin, role:admin")

    if not has_admin_policies:
        admin_rules = [
            "p, role:admin, namespaces, *, *",
            "p, role:admin, database-engines, *, */*",
            "p, role:admin, database-clusters, *, */*",
            "p, role:admin, database-cluster-backups, *, */*",
            "p, role:admin, database-cluster-restores, *, */*",
            "p, role:admin, database-cluster-credentials, *, */*",
            "p, role:admin, backup-storages, *, */*",
            "p, role:admin, monitoring-instances, *, */*",
        ]
        # Prepend to make intent explicit
        lines = admin_rules + lines

    # Rejoin with trailing newline
    out = "\n".join(lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


async def apply_policy_if_configured(username: str, namespace: str, timeout: int = 60) -> Dict[str, Any]:
    """
    Apply RBAC policy for the user/namespace during bootstrap.

    Behavior:
    - If EVEREST_RBAC_APPLY_CMD is set, write a temp policy.csv and execute it
      (supports {file} placeholder).
    - Otherwise, automatically manage the everest-rbac ConfigMap directly via
      kubectl: ensure enabled (if EVEREST_RBAC_ENABLE_ON_BOOTSTRAP is truthy),
      merge/update policy.csv with the generated role/binding for the user.
    Returns a step-like dict with rbac_applied and rbac_enabled flags.
    """
    policy = build_policy_csv(username, namespace)
    enable_flag = os.environ.get("EVEREST_RBAC_ENABLE_ON_BOOTSTRAP", "").lower() in ("1", "true", "yes", "on")
    rbac_enabled: Optional[bool] = None

    # If not enabled, skip entirely (do not modify ConfigMap)
    if not enable_flag:
        return {
            "name": "apply_rbac_policy",
            "command": "<skipped>",
            "exit_code": 0,
            "rbac_applied": False,
            "rbac_enabled": False,
            "stdout": "",
            "stderr": "RBAC not enabled; skipping",
        }

    # If requested, enable the RBAC ConfigMap first
    en = await run_cmd(
        [
            "kubectl",
            "-n",
            "everest-system",
            "patch",
            "configmap",
            "everest-rbac",
            "--type",
            "merge",
            "-p",
            '{"data":{"enabled":"true"}}',
        ],
        timeout=timeout,
    )
    rbac_enabled = en.get("exit_code") == 0

    cmd_tpl = os.environ.get("EVEREST_RBAC_APPLY_CMD")
    if cmd_tpl:
        # External command path (e.g., everestctl)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="policy_", suffix=".csv", delete=False) as tf:
                tf.write(policy.encode())
                temp_path = tf.name

            cmd_str = cmd_tpl.replace("{file}", temp_path)
            # Use shlex.split to preserve quoted arguments safely
            cmd = shlex.split(cmd_str)
            res = await run_cmd(cmd, timeout=timeout)
            res.update({
                "name": "apply_rbac_policy",
                "rbac_applied": res.get("exit_code", 1) == 0,
                "rbac_enabled": rbac_enabled,
            })
            return res
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

    # Built-in fallback: manage ConfigMap directly with kubectl
    get_res = await run_cmd(
        [
            "kubectl",
            "-n",
            "everest-system",
            "get",
            "configmap",
            "everest-rbac",
            "-o",
            "json",
        ],
        timeout=timeout,
    )
    create_new = get_res.get("exit_code") != 0
    enabled_val = "true" if enable_flag else "false"
    existing_policy = ""
    if not create_new:
        try:
            obj = json.loads(get_res.get("stdout", "{}"))
            data = obj.get("data", {})
            existing_policy = data.get("policy.csv", "")
            # Keep existing enabled unless we explicitly set it true
            if not enable_flag:
                enabled_val = data.get("enabled", "false")
        except Exception:
            # Treat as create-new if parse fails
            create_new = True

    # Build new policy by removing previous lines for this user and appending fresh ones
    def _filtered(existing: str) -> str:
        lines = [ln for ln in existing.splitlines()]
        new_lines = []
        user_role = f"role:{username}"
        for ln in lines:
            s = ln.strip()
            if not s:
                new_lines.append(ln)
                continue
            if s.startswith(f"g, {username}, "):
                continue
            if s.startswith("p, ") and (f", {user_role}," in s or s.startswith(f"p, {user_role},")):
                continue
            new_lines.append(ln)
        return "\n".join(new_lines).rstrip("\n")

    merged = _filtered(existing_policy)
    # Ensure newline separation if existing content remains
    if merged:
        merged = merged + "\n" + policy.rstrip("\n") + "\n"
    else:
        merged = policy.rstrip("\n") + "\n"

    # Ensure admin baseline policies exist
    merged = _ensure_admin_baseline(merged)

    indented_policy = "".join([("    " + ln + ("\n" if not ln.endswith("\n") else "")) for ln in merged.splitlines()])
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

    apply_res = await run_cmd([
        "kubectl",
        "apply",
        "-f",
        "-",
    ], input_text=manifest, timeout=timeout)
    apply_res.update({
        "name": "apply_rbac_policy",
        "rbac_applied": apply_res.get("exit_code", 1) == 0,
        "rbac_enabled": True if enable_flag else rbac_enabled,
        "command": apply_res.get("command"),
        "manifest_preview": manifest[:5000],
    })
    return apply_res


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
