import json
import os
import shlex
import tempfile
from typing import Any, Dict, Optional

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


def _prune_user_policy(existing_policy: str, username: str) -> str:
    """Remove any bindings or policies associated with the user."""

    user_role = f"role:{username}"
    kept_lines = []
    for raw_line in existing_policy.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            kept_lines.append(raw_line)
            continue
        if stripped.startswith(f"g, {username}, "):
            continue
        if stripped.startswith("p, ") and (
            f", {user_role}," in stripped or stripped.startswith(f"p, {user_role},")
        ):
            continue
        kept_lines.append(raw_line)
    return "\n".join(kept_lines).rstrip("\n")


def _ensure_admin_baseline(existing_policy: str) -> str:
    """Ensure the policy contains a functional admin baseline."""

    lines = existing_policy.splitlines()
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
        lines = admin_rules + lines

    out = "\n".join(lines).rstrip("\n")
    return out + "\n"


def _render_configmap_manifest(enabled_val: str, policy_body: str) -> str:
    """Render the ConfigMap manifest for the RBAC policy."""
    policy_lines = policy_body.splitlines()
    indented_policy = "\n".join(f"    {line}" for line in policy_lines)
    if indented_policy:
        indented_policy += "\n"
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
    return manifest


async def _load_rbac_configmap(timeout: int) -> tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[str]]:
    """Fetch the everest-rbac ConfigMap and return its parsed body."""

    res = await run_cmd(
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

    if res.get("exit_code") != 0:
        return res, None, None

    stdout = res.get("stdout", "")
    if not stdout.strip():
        return res, None, "empty ConfigMap payload"

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        return res, None, f"failed to parse ConfigMap JSON: {exc}"

    if not isinstance(parsed, dict):  # pragma: no cover - defensive
        return res, None, "unexpected ConfigMap payload type"

    return res, parsed, None


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
    get_res, configmap_obj, parse_error = await _load_rbac_configmap(timeout)
    enabled_val = "true" if enable_flag else "false"
    existing_policy = ""

    if configmap_obj:
        data = configmap_obj.get("data", {})
        existing_policy = data.get("policy.csv", "")
        if not enable_flag:
            enabled_val = data.get("enabled", "false")
    elif parse_error:
        # Treat parse errors as an empty baseline and recreate the manifest.
        existing_policy = ""

    merged = _prune_user_policy(existing_policy, username)
    # Ensure newline separation if existing content remains
    if merged:
        merged = merged + "\n" + policy.rstrip("\n") + "\n"
    else:
        merged = policy.rstrip("\n") + "\n"

    # Ensure admin baseline policies exist
    merged = _ensure_admin_baseline(merged)

    manifest = _render_configmap_manifest(enabled_val, merged)

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
    get_res, configmap_obj, parse_error = await _load_rbac_configmap(timeout)
    step = {
        "name": "revoke_rbac_user",
        "command": get_res.get("command", ""),
        "exit_code": get_res.get("exit_code", 1),
    }
    if get_res.get("exit_code") != 0:
        step.update(
            {
                "rbac_changed": False,
                "stdout": get_res.get("stdout", ""),
                "stderr": get_res.get("stderr", ""),
            }
        )
        return step

    if parse_error or not isinstance(configmap_obj, dict):
        error_msg = parse_error or "missing ConfigMap payload"
        return {
            "name": "revoke_rbac_user",
            "exit_code": 1,
            "rbac_changed": False,
            "stdout": "",
            "stderr": f"parse error: {error_msg}",
        }

    data = configmap_obj.get("data", {})
    enabled_val = data.get("enabled", "true")
    existing_policy = data.get("policy.csv", "")
    pruned_policy = _prune_user_policy(existing_policy, username)

    if pruned_policy == existing_policy.rstrip("\n"):
        return {
            "name": "revoke_rbac_user",
            "command": get_res.get("command", ""),
            "exit_code": 0,
            "rbac_changed": False,
            "stdout": "no changes needed",
            "stderr": "",
        }

    new_policy = pruned_policy + "\n" if pruned_policy else ""
    manifest = _render_configmap_manifest(enabled_val, new_policy)

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
