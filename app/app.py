import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field, ConfigDict, field_validator

from .execs import run_cmd
from .jobs import JobStore, utcnow_iso
from .k8s import build_quota_limitrange_yaml, build_scale_statefulsets_cmd
from .parsers import parse_accounts_output
from .rbac import apply_policy_if_configured, revoke_user_in_rbac_configmap
from .logging_utils import configure_logging, correlation_middleware


configure_logging()
logger = logging.getLogger("everestctl_api")

# API key(s)
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "changeme")
# Optional multi-key support: JSON map of {kid: key}
import json as _json  # local alias to avoid name clash
try:
    _ADMIN_KEYS_JSON = os.environ.get("ADMIN_API_KEYS_JSON")
    ADMIN_API_KEYS: Optional[Dict[str, str]] = _json.loads(_ADMIN_KEYS_JSON) if _ADMIN_KEYS_JSON else None
except Exception:
    ADMIN_API_KEYS = None

app = FastAPI(title="Everest Bootstrap API", version="1.0.0")
# Correlation/JSON access logs
app.middleware("http")(correlation_middleware)
jobs = JobStore()


def _mask_command(cmd_str: str) -> str:
    """Mask password flags in a joined command string for safe logging."""
    parts = cmd_str.split()
    out = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("-p", "--password") and i + 1 < len(parts):
            out.append(p)
            out.append("********")
            i += 2
            continue
        if p.startswith("--password="):
            out.append("--password=********")
            i += 1
            continue
        out.append(p)
        i += 1
    return " ".join(out)


def _preview_text(text: Optional[str], limit: int = 600) -> str:
    """Return a short, friendly preview for logs.
    - Drops carriage returns to avoid spinner spam.
    - Truncates to the last `limit` characters.
    - If nothing meaningful, returns empty string.
    """
    if not text:
        return ""
    s = text.replace("\r", "")
    s = s.strip()
    if len(s) <= limit:
        return s
    tail = s[-limit:]
    omitted = len(s) - limit
    return f"...omitted {omitted} chars...\n{tail}"


async def require_admin_key(
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    x_admin_kid: Optional[str] = Header(None, alias="X-Admin-Key-Id"),
) -> None:
    # Support single key (default) and optional multi-key with kid
    if ADMIN_API_KEYS:
        if not (x_admin_kid and x_admin_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        expected = ADMIN_API_KEYS.get(x_admin_kid)
        if not expected or x_admin_key != expected:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        return
    # Fallback single key
    if not x_admin_key or x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_NAMESPACE_DENYLIST = {"kube-system", "kube-public", "default", "everest-system", "kube-node-lease"}


def _validate_k8s_name(value: str, field_name: str) -> str:
    if not _K8S_NAME_RE.match(value):
        raise ValueError(f"{field_name} must match RFC 1123 label (lowercase alphanumerics and -)")
    if field_name == "namespace" and value in _NAMESPACE_DENYLIST:
        raise ValueError("namespace is not allowed")
    # Optional allowed prefixes
    prefixes = [p.strip() for p in os.environ.get("ALLOWED_NAMESPACE_PREFIXES", "").split(",") if p.strip()]
    if field_name == "namespace" and prefixes:
        if not any(value.startswith(p) for p in prefixes):
            raise ValueError("namespace prefix not allowed")
    return value


class OperatorFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mongodb: bool = False
    postgresql: bool = False
    xtradb_cluster: bool = False
    mysql: Optional[bool] = None  # newer CLI


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_cores: int = Field(2, ge=1, le=128)
    ram_mb: int = Field(2048, ge=128, le=1048576)
    disk_gb: int = Field(20, ge=1, le=1048576)
    # Optional: set a hard cap on the number of database cluster CRs
    # via ResourceQuota count/<resource> (see EVEREST_DB_COUNT_RESOURCES)
    max_databases: Optional[int] = Field(default=None, ge=0, le=10000)


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=63)
    namespace: Optional[str] = Field(default=None, min_length=1, max_length=63)
    operators: OperatorFlags = Field(default_factory=OperatorFlags)
    take_ownership: bool = False
    resources: Resources = Field(default_factory=Resources)
    # Optional initial password. If omitted, the API uses
    # BOOTSTRAP_DEFAULT_PASSWORD env or generates a strong one.
    password: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _val_user(cls, v: str) -> str:
        return _validate_k8s_name(v, "username")

    @field_validator("namespace")
    @classmethod
    def _val_ns(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_k8s_name(v, "namespace")


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=63)
    new_password: str = Field(..., min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def _val_user(cls, v: str) -> str:
        return _validate_k8s_name(v, "username")


class NamespaceResourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(..., min_length=1, max_length=63)
    resources: Resources = Field(default_factory=Resources)

    @field_validator("namespace")
    @classmethod
    def _val_ns(cls, v: str) -> str:
        return _validate_k8s_name(v, "namespace")


class NamespaceOperatorsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str = Field(..., min_length=1, max_length=63)
    operators: OperatorFlags = Field(default_factory=OperatorFlags)

    @field_validator("namespace")
    @classmethod
    def _val_ns(cls, v: str) -> str:
        return _validate_k8s_name(v, "namespace")


class SuspendUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=63)
    namespace: Optional[str] = Field(default=None, min_length=1, max_length=63)
    scale_statefulsets: bool = True
    revoke_rbac: bool = True

    @field_validator("username")
    @classmethod
    def _val_user(cls, v: str) -> str:
        return _validate_k8s_name(v, "username")

    @field_validator("namespace")
    @classmethod
    def _val_ns(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_k8s_name(v, "namespace")


class DeleteUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=63)
    namespace: Optional[str] = Field(default=None, min_length=1, max_length=63)

    @field_validator("username")
    @classmethod
    def _val_user(cls, v: str) -> str:
        return _validate_k8s_name(v, "username")

    @field_validator("namespace")
    @classmethod
    def _val_ns(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_k8s_name(v, "namespace")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/readyz")
async def readyz():
    return {"ok": True}


@app.post("/bootstrap/users", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_admin_key)])
async def submit_bootstrap(req: BootstrapRequest, background: BackgroundTasks):
    ns = req.namespace or req.username
    job = await jobs.create()
    logger.info(
        "job created",
        extra={
            "event": "job_created",
            "job_id": job.job_id,
            "username": req.username,
            "namespace": ns,
        },
    )

    async def _run():
        await jobs.update(job.job_id, status="running", started_at=utcnow_iso())
        logger.info(
            "job started",
            extra={"event": "job_started", "job_id": job.job_id, "username": req.username, "namespace": ns},
        )
        steps = []
        inputs = {
            "username": req.username,
            "namespace": ns,
            "operators": req.operators.model_dump(),
            "take_ownership": req.take_ownership,
            "resources": req.resources.model_dump(),
        }

        overall_status = "succeeded"
        summary = []

        # Track if we generated a password to return it in the result
        generated_password = None

        try:
            # Step 1: Create account (supply password to avoid TTY prompts)
            import secrets

            default_pw_env = os.environ.get("BOOTSTRAP_DEFAULT_PASSWORD")
            chosen_password = req.password or default_pw_env
            if not chosen_password:
                generated_password = secrets.token_urlsafe(16)
                chosen_password = generated_password

            step1_cmd = [
                "everestctl",
                "accounts",
                "create",
                "-u",
                req.username,
                "-p",
                chosen_password,
            ]
            res1 = await run_cmd(step1_cmd, timeout=60)
            res1.update({"name": "create_account"})
            # Mask secrets in the logged command
            if "command" in res1:
                res1["command"] = _mask_command(res1["command"])  # type: ignore
            steps.append(res1)
            logger.info(
                "step",
                extra={
                    "event": "job_step",
                    "job_id": job.job_id,
                    "step_name": "create_account",
                    "command": res1.get("command"),
                    "exit_code": res1.get("exit_code"),
                    "stdout_preview": _preview_text(res1.get("stdout")),
                    "stderr_preview": _preview_text(res1.get("stderr")),
                },
            )
            if res1.get("exit_code") != 0:
                overall_status = "failed"
                summary.append("account creation failed")

            # Step 2: Create or take ownership of namespace (attempt newer CLI first)
            if overall_status == "succeeded":
                operators = req.operators
                # Determine effective operator selections. If none selected in the
                # request, fall back to BOOTSTRAP_DEFAULT_OPERATORS env (default: postgresql)
                def_ops = os.environ.get("BOOTSTRAP_DEFAULT_OPERATORS", "postgresql")
                def_set = {s.strip().lower() for s in def_ops.split(",") if s.strip()}
                enable_mongodb = bool(operators.mongodb)
                enable_postgresql = bool(operators.postgresql)
                # mysql flag may be None on older/newer CLI; we derive an intent
                want_mysql_like = (
                    (operators.mysql is True)
                    or operators.xtradb_cluster
                    or ("mysql" in def_set)
                    or ("xtradb_cluster" in def_set)
                    or ("xtradb-cluster" in def_set)
                )
                # If nothing selected, enable defaults
                if not any([enable_mongodb, enable_postgresql, want_mysql_like]):
                    enable_postgresql = True  # safe default
                new_cli_cmd = [
                    "everestctl",
                    "namespaces",
                    "add",
                    ns,
                    f"--operator.mongodb={'true' if enable_mongodb else 'false'}",
                    f"--operator.postgresql={'true' if enable_postgresql else 'false'}",
                ]
                # Prefer new mysql flag; fallback to xtradb in case of unknown flag
                new_cli_cmd.append(f"--operator.mysql={'true' if want_mysql_like else 'false'}")
                if req.take_ownership:
                    new_cli_cmd.append("--take-ownership")

                res2 = await run_cmd(new_cli_cmd, timeout=120)
                res2.update({"name": "add_namespace"})
                # Fallback to older flag if unknown option
                if res2.get("exit_code") != 0 and ("unknown flag" in res2.get("stderr", "").lower() or "unknown flag" in res2.get("stdout", "").lower()):
                    old_cli_cmd = [
                        "everestctl",
                        "namespaces",
                        "add",
                        ns,
                        f"--operator.mongodb={'true' if enable_mongodb else 'false'}",
                        f"--operator.postgresql={'true' if enable_postgresql else 'false'}",
                        f"--operator.xtradb-cluster={'true' if want_mysql_like else 'false'}",
                    ]
                    if req.take_ownership:
                        old_cli_cmd.append("--take-ownership")
                    res2 = await run_cmd(old_cli_cmd, timeout=120)
                    res2.update({"name": "add_namespace"})

                steps.append(res2)
                logger.info(
                    "step",
                    extra={
                        "event": "job_step",
                        "job_id": job.job_id,
                        "step_name": "add_namespace",
                        "command": res2.get("command"),
                        "exit_code": res2.get("exit_code"),
                        "stdout_preview": _preview_text(res2.get("stdout")),
                        "stderr_preview": _preview_text(res2.get("stderr")),
                    },
                )
                if res2.get("exit_code") != 0:
                    overall_status = "failed"
                    summary.append("namespace add failed")

            # Step 3: Apply ResourceQuota & LimitRange
            if overall_status == "succeeded":
                manifest = build_quota_limitrange_yaml(ns, req.resources.model_dump())
                res3 = await run_cmd([
                    "kubectl",
                    "apply",
                    "-n",
                    ns,
                    "-f",
                    "-",
                ], input_text=manifest, timeout=90)
                res3.update({
                    "name": "apply_resource_quota",
                    "manifest_preview": manifest,
                })
                steps.append(res3)
                logger.info(
                    "step",
                    extra={
                        "event": "job_step",
                        "job_id": job.job_id,
                        "step_name": "apply_resource_quota",
                        "command": res3.get("command"),
                        "exit_code": res3.get("exit_code"),
                        "stdout_preview": _preview_text(res3.get("stdout")),
                        "stderr_preview": _preview_text(res3.get("stderr")),
                    },
                )
                if res3.get("exit_code") != 0:
                    overall_status = "failed"
                    summary.append("quota/limits apply failed")

            # Step 4: RBAC policy (if configured)
            rbac_step = await apply_policy_if_configured(req.username, ns, timeout=90)
            steps.append(rbac_step)
            logger.info(
                "step",
                extra={
                    "event": "job_step",
                    "job_id": job.job_id,
                    "step_name": "apply_rbac_policy",
                    "command": rbac_step.get("command"),
                    "exit_code": rbac_step.get("exit_code"),
                    "stdout_preview": _preview_text(rbac_step.get("stdout")),
                    "stderr_preview": _preview_text(rbac_step.get("stderr")),
                },
            )

            # Summarize
            if overall_status == "succeeded" and rbac_step.get("rbac_applied"):
                summary.append(f"User {req.username} and namespace {ns} created; quota applied; role bound.")
            elif overall_status == "succeeded":
                summary.append(f"User {req.username} and namespace {ns} created; quota applied; RBAC skipped.")
        except Exception as e:
            overall_status = "failed"
            steps.append({
                "name": "internal_error",
                "command": "<internal>",
                "exit_code": 1,
                "stdout": "",
                "stderr": str(e),
            })
            logger.exception("Bootstrap job failed")
            summary.append("internal error")
        finally:
            # Build result payload and include generated credentials (if any)
            result_payload = {
                "inputs": inputs,
                "steps": steps,
                "overall_status": overall_status,
                "summary": "; ".join(summary) if summary else overall_status,
            }
            if generated_password:
                result_payload["credentials"] = {"username": req.username, "password": generated_password}

            await jobs.update(
                job.job_id,
                status=overall_status,
                finished_at=utcnow_iso(),
                summary="; ".join(summary) if summary else overall_status,
                result=result_payload,
            )
            logger.info(
                "job finished",
                extra={
                    "event": "job_finished",
                    "job_id": job.job_id,
                    "status": overall_status,
                    "summary": "; ".join(summary) if summary else overall_status,
                },
            )

    background.add_task(_run)
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_admin_key)])
async def job_status(job_id: str):
    data = await jobs.serialize(job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    return data


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_admin_key)])
async def job_result(job_id: str):
    job = await jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("succeeded", "failed"):
        raise HTTPException(status_code=409, detail="job not finished")
    return job.result


@app.get("/accounts/list", dependencies=[Depends(require_admin_key)])
async def accounts_list():
    # Try JSON first
    res = await run_cmd(["everestctl", "accounts", "list", "--json"], timeout=30)
    if res["exit_code"] == 0:
        try:
            parsed = parse_accounts_output(res["stdout"])  # json path
            return parsed
        except Exception:
            pass

    # Fallback without --json
    res = await run_cmd(["everestctl", "accounts", "list"], timeout=30)
    if res["exit_code"] == 0:
        return parse_accounts_output(res["stdout"])  # attempt to parse table

    # Some versions may use singular 'account'
    res2 = await run_cmd(["everestctl", "account", "list"], timeout=30)
    if res2["exit_code"] == 0:
        return parse_accounts_output(res2["stdout"])  # attempt to parse table

    # Failure
    detail = res.get("stderr") or res2.get("stderr") or "everestctl failed"
    raise HTTPException(status_code=502, detail={"error": "everestctl failed", "detail": detail[-4000:]})


@app.post("/accounts/password", dependencies=[Depends(require_admin_key)])
async def set_account_password(req: PasswordChangeRequest):
    """
    Change a user's password using everestctl. Many versions prompt for the
    password on stdin; provide it twice (password + confirmation).
    """
    # Attempt non-interactive first if supported (unknown; keep stdin flow as primary)
    input_text = f"{req.new_password}\n{req.new_password}\n"
    res = await run_cmd([
        "everestctl",
        "accounts",
        "set-password",
        "-u",
        req.username,
    ], timeout=60, input_text=input_text)
    if res.get("exit_code") != 0:
        # Fallback to flag-based non-interactive if stdin/TTY is not supported by this version
        res2 = await run_cmd([
            "everestctl",
            "accounts",
            "set-password",
            "-u",
            req.username,
            "-p",
            req.new_password,
        ], timeout=60)
        if "command" in res2:
            res2["command"] = _mask_command(res2["command"])  # type: ignore
        if res2.get("exit_code") != 0:
            detail = (res2.get("stderr") or res.get("stderr") or "")[-4000:]
            raise HTTPException(status_code=502, detail={"error": "set-password failed", "detail": detail})
    return {"ok": True, "username": req.username}


@app.post("/namespaces/resources", dependencies=[Depends(require_admin_key)])
async def update_namespace_resources(req: NamespaceResourceUpdate):
    """
    Apply or update ResourceQuota and LimitRange for a namespace.
    """
    manifest = build_quota_limitrange_yaml(req.namespace, req.resources.model_dump())
    res = await run_cmd([
        "kubectl",
        "apply",
        "-n",
        req.namespace,
        "-f",
        "-",
    ], input_text=manifest, timeout=90)
    if res.get("exit_code") != 0:
        raise HTTPException(status_code=502, detail={"error": "kubectl apply failed", "detail": res.get("stderr", "")[-4000:]})
    return {"ok": True, "namespace": req.namespace, "applied": True}


@app.post("/namespaces/operators", dependencies=[Depends(require_admin_key)])
async def update_namespace_operators(req: NamespaceOperatorsUpdate):
    """
    Enable database operators for a namespace using everestctl. Supports both
    new (--operator.mysql) and older (--operator.xtradb-cluster) flags.
    """
    ops = req.operators
    new_cli_cmd = [
        "everestctl",
        "namespaces",
        "update",
        req.namespace,
        f"--operator.mongodb={'true' if ops.mongodb else 'false'}",
        f"--operator.postgresql={'true' if ops.postgresql else 'false'}",
    ]
    if ops.mysql is not None:
        new_cli_cmd.append(f"--operator.mysql={'true' if ops.mysql else 'false'}")
    else:
        new_cli_cmd.append("--operator.mysql=false")
    res = await run_cmd(new_cli_cmd, timeout=120)

    # Fallback to older CLI flag if mysql is unknown
    if res.get("exit_code") != 0 and ("unknown flag" in res.get("stderr", "").lower() or "unknown flag" in res.get("stdout", "").lower()):
        old_cli_cmd = [
            "everestctl",
            "namespaces",
            "update",
            req.namespace,
            f"--operator.mongodb={'true' if ops.mongodb else 'false'}",
            f"--operator.postgresql={'true' if ops.postgresql else 'false'}",
            f"--operator.xtradb-cluster={'true' if ops.xtradb_cluster else 'false'}",
        ]
        res = await run_cmd(old_cli_cmd, timeout=120)

    if res.get("exit_code") != 0:
        raise HTTPException(status_code=502, detail={"error": "namespaces update failed", "detail": res.get("stderr", "")[-4000:]})
    return {"ok": True, "namespace": req.namespace}


@app.post("/accounts/suspend", dependencies=[Depends(require_admin_key)])
async def suspend_user(req: SuspendUserRequest):
    ns = req.namespace or req.username
    steps = []

    # Step 1: deactivate/disable/suspend account via everestctl (try variants)
    deactivate_variants = [
        ["everestctl", "accounts", "deactivate", "-u", req.username],
        ["everestctl", "accounts", "disable", "-u", req.username],
        ["everestctl", "accounts", "suspend", "-u", req.username],
        ["everestctl", "accounts", "lock", "-u", req.username],
    ]
    acct_res = None
    for variant in deactivate_variants:
        r = await run_cmd(variant, timeout=60)
        r.update({"name": "deactivate_account"})
        steps.append(r)
        if r.get("exit_code") == 0:
            acct_res = r
            break
    # Step 2: scale down DB workloads
    scale_res = None
    if req.scale_statefulsets:
        scale_cmd = build_scale_statefulsets_cmd(ns)
        scale_res = await run_cmd(scale_cmd, timeout=90)
        scale_res.update({"name": "scale_down_statefulsets"})
        steps.append(scale_res)
    # Step 3: revoke RBAC
    rbac_res = None
    if req.revoke_rbac:
        rbac_res = await revoke_user_in_rbac_configmap(req.username, timeout=90)
        steps.append(rbac_res)

    # Consider suspend successful if at least one remediation succeeded
    scale_ok = (not req.scale_statefulsets) or (scale_res is not None and scale_res.get("exit_code") == 0)
    rbac_ok = (not req.revoke_rbac) or (rbac_res is not None and rbac_res.get("exit_code") == 0)
    overall_ok = bool(scale_ok or rbac_ok)

    return {"ok": overall_ok, "username": req.username, "namespace": ns, "steps": steps}


@app.post("/accounts/delete", dependencies=[Depends(require_admin_key)])
async def delete_user(req: DeleteUserRequest):
    ns = req.namespace or req.username
    steps = []

    # Step 1: remove namespace (prefer everestctl, fallback to kubectl)
    rm_ns = await run_cmd(["everestctl", "namespaces", "remove", ns], timeout=180)
    rm_ns.update({"name": "remove_namespace"})
    steps.append(rm_ns)
    if rm_ns.get("exit_code") != 0 and os.environ.get("ENABLE_K8S_NAMESPACE_DELETE_FALLBACK", "").lower() in ("1", "true", "yes", "on"):
        # fallback to kubectl delete namespace
        k8s_del = await run_cmd(["kubectl", "delete", "namespace", ns, "--ignore-not-found=true"], timeout=180)
        k8s_del.update({"name": "delete_namespace"})
        steps.append(k8s_del)

    # Step 2: revoke RBAC entries
    rbac_res = await revoke_user_in_rbac_configmap(req.username, timeout=90)
    steps.append(rbac_res)

    # Step 3: delete account (try delete/remove)
    del_cmds = [
        ["everestctl", "accounts", "delete", "-u", req.username],
        ["everestctl", "accounts", "remove", "-u", req.username],
    ]
    acct_del_res = None
    for cmd in del_cmds:
        r = await run_cmd(cmd, timeout=60)
        r.update({"name": "delete_account"})
        steps.append(r)
        if r.get("exit_code") == 0:
            acct_del_res = r
            break

    overall_ok = True
    # Consider operation ok if namespace removal via either method succeeded and account deletion succeeded
    ns_ok = any(s.get("name") in ("remove_namespace", "delete_namespace") and s.get("exit_code") == 0 for s in steps)
    acct_ok = acct_del_res is not None and acct_del_res.get("exit_code") == 0
    if not ns_ok or not acct_ok:
        overall_ok = False

    return {"ok": overall_ok, "username": req.username, "namespace": ns, "steps": steps}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics exposition."""
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    except Exception:
        raise HTTPException(status_code=503, detail="prometheus_client not installed")
    data = generate_latest()  # type: ignore
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
