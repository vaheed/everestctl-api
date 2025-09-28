import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

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


@dataclass
class StepOutcome:
    """Helper structure encapsulating a bootstrap step outcome."""

    result: Dict[str, Any]
    succeeded: bool
    failure_summary: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def _log_job_step(job_id: str, step: Dict[str, Any]) -> None:
    """Emit a structured log line describing a job step outcome."""

    logger.info(
        "step",
        extra={
            "event": "job_step",
            "job_id": job_id,
            "step_name": step.get("name"),
            "command": step.get("command"),
            "exit_code": step.get("exit_code"),
            "stdout_preview": _preview_text(step.get("stdout")),
            "stderr_preview": _preview_text(step.get("stderr")),
        },
    )


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
    delete_account: bool = True

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


async def _create_account(req: BootstrapRequest) -> StepOutcome:
    """Create an Everest account, handling idempotent outcomes."""

    import secrets

    default_pw_env = os.environ.get("BOOTSTRAP_DEFAULT_PASSWORD")
    chosen_password = req.password or default_pw_env
    generated_password: Optional[str] = None
    if not chosen_password:
        generated_password = secrets.token_urlsafe(16)
        chosen_password = generated_password

    cmd = [
        "everestctl",
        "accounts",
        "create",
        "-u",
        req.username,
        "-p",
        chosen_password,
    ]
    res = await run_cmd(cmd, timeout=60)
    res.update({"name": "create_account"})
    if "command" in res:
        res["command"] = _mask_command(res["command"])  # type: ignore[index]

    succeeded = res.get("exit_code") == 0
    account_existed = False
    failure_summary: Optional[str] = None

    meta = {
        "generated_password": generated_password,
        "account_existed": account_existed,
    }

    if not succeeded:
        msg = (res.get("stderr", "") + res.get("stdout", "")).lower()
        if "already exists" in msg or "user exists" in msg or "exists" in msg:
            res["exit_code"] = 0
            res["stdout"] = (
                res.get("stdout", "")
                + ("\n" if res.get("stdout") else "")
                + "user already exists; treated as success"
            ).strip()
            res["stderr"] = ""
            account_existed = True
            succeeded = True
            meta.update({
                "account_existed": True,
                "log_adjustment": {
                    "step_name": "create_account",
                    "note": "treated as success: already exists",
                },
            })
        else:
            failure_summary = "account creation failed"

    return StepOutcome(
        result=res,
        succeeded=succeeded,
        failure_summary=failure_summary,
        meta=meta,
    )


async def _ensure_namespace(req: BootstrapRequest, namespace: str) -> StepOutcome:
    """Ensure the target namespace exists with the correct operators enabled."""

    operators = req.operators
    def_ops = os.environ.get("BOOTSTRAP_DEFAULT_OPERATORS", "postgresql")
    def_set = {s.strip().lower() for s in def_ops.split(",") if s.strip()}
    enable_mongodb = bool(operators.mongodb)
    enable_postgresql = bool(operators.postgresql)
    want_mysql_like = (
        (operators.mysql is True)
        or operators.xtradb_cluster
        or ("mysql" in def_set)
        or ("xtradb_cluster" in def_set)
        or ("xtradb-cluster" in def_set)
    )
    if not any([enable_mongodb, enable_postgresql, want_mysql_like]):
        enable_postgresql = True

    new_cli_cmd = [
        "everestctl",
        "namespaces",
        "add",
        namespace,
        f"--operator.mongodb={'true' if enable_mongodb else 'false'}",
        f"--operator.postgresql={'true' if enable_postgresql else 'false'}",
    ]
    new_cli_cmd.append(f"--operator.mysql={'true' if want_mysql_like else 'false'}")
    if req.take_ownership:
        new_cli_cmd.append("--take-ownership")

    res = await run_cmd(new_cli_cmd, timeout=120)
    res.update({"name": "add_namespace"})

    if res.get("exit_code") != 0 and (
        "unknown flag" in res.get("stderr", "").lower()
        or "unknown flag" in res.get("stdout", "").lower()
    ):
        old_cli_cmd = [
            "everestctl",
            "namespaces",
            "add",
            namespace,
            f"--operator.mongodb={'true' if enable_mongodb else 'false'}",
            f"--operator.postgresql={'true' if enable_postgresql else 'false'}",
            f"--operator.xtradb-cluster={'true' if want_mysql_like else 'false'}",
        ]
        if req.take_ownership:
            old_cli_cmd.append("--take-ownership")
        res = await run_cmd(old_cli_cmd, timeout=120)
        res.update({"name": "add_namespace"})

    succeeded = res.get("exit_code") == 0
    namespace_existed = False
    failure_summary: Optional[str] = None
    meta = {"namespace_existed": namespace_existed}

    if not succeeded:
        msg = (res.get("stderr", "") + res.get("stdout", "")).lower()
        if "already exists" in msg or "exists" in msg or "already present" in msg:
            res["exit_code"] = 0
            res["stdout"] = (
                res.get("stdout", "")
                + ("\n" if res.get("stdout") else "")
                + f"namespace '{namespace}' already exists; treated as success"
            ).strip()
            res["stderr"] = ""
            namespace_existed = True
            succeeded = True
            meta.update(
                {
                    "namespace_existed": True,
                    "log_adjustment": {
                        "step_name": "add_namespace",
                        "note": "treated as success: already exists",
                    },
                }
            )
        else:
            failure_summary = "namespace add failed"

    return StepOutcome(
        result=res,
        succeeded=succeeded,
        failure_summary=failure_summary,
        meta=meta,
    )


async def _apply_resource_quota(namespace: str, resources: Dict[str, Any]) -> StepOutcome:
    """Apply the ResourceQuota and LimitRange manifest for the namespace."""

    manifest = build_quota_limitrange_yaml(namespace, resources)
    res = await run_cmd(
        [
            "kubectl",
            "apply",
            "-n",
            namespace,
            "-f",
            "-",
        ],
        input_text=manifest,
        timeout=90,
    )
    res.update({"name": "apply_resource_quota", "manifest_preview": manifest})

    succeeded = res.get("exit_code") == 0
    failure_summary = None if succeeded else "quota/limits apply failed"

    return StepOutcome(result=res, succeeded=succeeded, failure_summary=failure_summary)


async def _apply_rbac_policy(username: str, namespace: str) -> StepOutcome:
    """Apply RBAC policy if configured. Does not impact job success if it fails."""

    res = await apply_policy_if_configured(username, namespace, timeout=90)
    return StepOutcome(result=res, succeeded=True)


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
        steps: list[Dict[str, Any]] = []
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

        # Track idempotency outcomes for clear summary wording
        account_existed = False
        namespace_existed = False

        try:
            async def _log_and_record(outcome: StepOutcome) -> None:
                steps.append(outcome.result)
                logger.info(
                    "step",
                    extra={
                        "event": "job_step",
                        "job_id": job.job_id,
                        "step_name": outcome.result.get("name"),
                        "command": outcome.result.get("command"),
                        "exit_code": outcome.result.get("exit_code"),
                        "stdout_preview": _preview_text(outcome.result.get("stdout")),
                        "stderr_preview": _preview_text(outcome.result.get("stderr")),
                    },
                )

            steps_plan: list[
                tuple[
                    str,
                    Callable[[], Awaitable[StepOutcome]],
                    Callable[[str], bool],
                    bool,
                ]
            ] = [
                (
                    "create_account",
                    lambda: _create_account(req),
                    lambda status: True,
                    True,
                ),
                (
                    "add_namespace",
                    lambda: _ensure_namespace(req, ns),
                    lambda status: status == "succeeded",
                    True,
                ),
                (
                    "apply_resource_quota",
                    lambda: _apply_resource_quota(ns, req.resources.model_dump()),
                    lambda status: status == "succeeded",
                    True,
                ),
                (
                    "apply_rbac_policy",
                    lambda: _apply_rbac_policy(req.username, ns),
                    lambda status: True,
                    False,
                ),
            ]

            for _step_name, step_factory, should_run, affects_status in steps_plan:
                if not should_run(overall_status):
                    continue
                outcome: StepOutcome = await step_factory()
                await _log_and_record(outcome)
                adjustment = outcome.meta.get("log_adjustment")
                if adjustment:
                    logger.info(
                        "step",
                        extra={
                            "event": "job_step_adjusted",
                            "job_id": job.job_id,
                            **adjustment,
                        },
                    )
                if (
                    affects_status
                    and not outcome.succeeded
                    and overall_status == "succeeded"
                ):
                    overall_status = "failed"
                    if outcome.failure_summary:
                        summary.append(outcome.failure_summary)
                account_existed = account_existed or outcome.meta.get("account_existed", False)
                namespace_existed = namespace_existed or outcome.meta.get("namespace_existed", False)
                if "generated_password" in outcome.meta and outcome.meta["generated_password"]:
                    generated_password = outcome.meta["generated_password"]

            rbac_step = steps[-1] if steps else {}

            # Summarize
            if overall_status == "succeeded":
                user_word = "created"
                ns_word = "created"
                if account_existed:
                    user_word = "existed"
                if namespace_existed:
                    ns_word = "existed"
                if rbac_step.get("rbac_applied"):
                    summary.append(
                        f"User {req.username} {user_word}; namespace {ns} {ns_word}; quota applied; role bound."
                    )
                else:
                    summary.append(
                        f"User {req.username} {user_word}; namespace {ns} {ns_word}; quota applied; RBAC skipped."
                    )
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


async def _set_account_password_job(job_id: str, req: PasswordChangeRequest) -> None:
    await jobs.update(job_id, status="running", started_at=utcnow_iso())
    logger.info(
        "job started",
        extra={"event": "job_started", "job_id": job_id, "username": req.username},
    )

    steps: list[Dict[str, Any]] = []
    inputs = {"username": req.username}
    overall_status = "succeeded"
    error_detail: Optional[str] = None

    try:
        input_text = f"{req.new_password}\n{req.new_password}\n"
        primary = await run_cmd(
            [
                "everestctl",
                "accounts",
                "set-password",
                "-u",
                req.username,
            ],
            timeout=60,
            input_text=input_text,
        )
        primary.update({"name": "set_password_stdin"})
        steps.append(primary)
        _log_job_step(job_id, primary)

        if primary.get("exit_code") != 0:
            fallback = await run_cmd(
                [
                    "everestctl",
                    "accounts",
                    "set-password",
                    "-u",
                    req.username,
                    "-p",
                    req.new_password,
                ],
                timeout=60,
            )
            if "command" in fallback:
                fallback["command"] = _mask_command(fallback["command"])  # type: ignore[index]
            fallback.update({"name": "set_password_flag"})
            steps.append(fallback)
            _log_job_step(job_id, fallback)

            if fallback.get("exit_code") != 0:
                overall_status = "failed"
                detail = (fallback.get("stderr") or primary.get("stderr") or "")
                error_detail = detail[-4000:] if detail else None
        # Success path falls through
    except Exception as exc:  # pragma: no cover - defensive
        overall_status = "failed"
        error_detail = str(exc)
        step = {
            "name": "internal_error",
            "command": "<internal>",
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
        }
        steps.append(step)
        _log_job_step(job_id, step)
        logger.exception("Password job failed", extra={"event": "job_failed", "job_id": job_id})
    finally:
        summary_text = (
            f"Password updated for {req.username}"
            if overall_status == "succeeded"
            else f"Failed to update password for {req.username}"
        )
        result_payload: Dict[str, Any] = {
            "ok": overall_status == "succeeded",
            "username": req.username,
            "inputs": inputs,
            "steps": steps,
            "overall_status": overall_status,
            "summary": summary_text,
        }
        if error_detail:
            result_payload["error_detail"] = error_detail

        await jobs.update(
            job_id,
            status=overall_status,
            finished_at=utcnow_iso(),
            summary=summary_text,
            result=result_payload,
        )
        logger.info(
            "job finished",
            extra={
                "event": "job_finished",
                "job_id": job_id,
                "status": overall_status,
                "summary": summary_text,
            },
        )


async def _update_namespace_resources_job(job_id: str, req: NamespaceResourceUpdate) -> None:
    await jobs.update(job_id, status="running", started_at=utcnow_iso())
    logger.info(
        "job started",
        extra={"event": "job_started", "job_id": job_id, "namespace": req.namespace},
    )

    steps: list[Dict[str, Any]] = []
    overall_status = "succeeded"
    outcome_summary = f"Resource quota applied for namespace {req.namespace}"
    inputs = {
        "namespace": req.namespace,
        "resources": req.resources.model_dump(),
    }

    try:
        outcome = await _apply_resource_quota(req.namespace, req.resources.model_dump())
        steps.append(outcome.result)
        _log_job_step(job_id, outcome.result)
        if not outcome.succeeded:
            overall_status = "failed"
            outcome_summary = outcome.failure_summary or "Resource quota apply failed"
    except Exception as exc:  # pragma: no cover - defensive
        overall_status = "failed"
        outcome_summary = "Resource quota apply failed"
        step = {
            "name": "internal_error",
            "command": "<internal>",
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
        }
        steps.append(step)
        _log_job_step(job_id, step)
        logger.exception(
            "Namespace resources job failed",
            extra={"event": "job_failed", "job_id": job_id},
        )
    finally:
        result_payload = {
            "ok": overall_status == "succeeded",
            "namespace": req.namespace,
            "applied": overall_status == "succeeded",
            "inputs": inputs,
            "steps": steps,
            "overall_status": overall_status,
            "summary": outcome_summary,
        }
        await jobs.update(
            job_id,
            status=overall_status,
            finished_at=utcnow_iso(),
            summary=outcome_summary,
            result=result_payload,
        )
        logger.info(
            "job finished",
            extra={
                "event": "job_finished",
                "job_id": job_id,
                "status": overall_status,
                "summary": outcome_summary,
            },
        )


async def _update_namespace_operators_once(
    req: NamespaceOperatorsUpdate,
) -> StepOutcome:
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

    used_legacy_cli = False
    if res.get("exit_code") != 0 and (
        "unknown flag" in res.get("stderr", "").lower()
        or "unknown flag" in res.get("stdout", "").lower()
    ):
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
        used_legacy_cli = True

    res.update({"name": "update_namespace_operators"})
    succeeded = res.get("exit_code") == 0
    failure_summary: Optional[str] = None
    meta: Dict[str, Any] = {"used_legacy_cli": used_legacy_cli}

    if not succeeded:
        msg = (res.get("stderr", "") + "\n" + res.get("stdout", "")).strip()
        lowered = msg.lower()
        if "another operation" in lowered and "in progress" in lowered:
            meta["transient_conflict"] = True
            failure_summary = "Another operation is in progress"
        else:
            failure_summary = "Namespace operators update failed"
        if msg:
            meta["failure_message"] = msg[-4000:]

    return StepOutcome(result=res, succeeded=succeeded, failure_summary=failure_summary, meta=meta)


async def _update_namespace_operators_job(job_id: str, req: NamespaceOperatorsUpdate) -> None:
    await jobs.update(job_id, status="running", started_at=utcnow_iso())
    logger.info(
        "job started",
        extra={"event": "job_started", "job_id": job_id, "namespace": req.namespace},
    )

    steps: list[Dict[str, Any]] = []
    overall_status = "succeeded"
    summary_text = f"Operators updated for namespace {req.namespace}"
    conflict_backoff = 20
    max_attempts = 3
    attempt = 0
    last_failure_message: Optional[str] = None

    try:
        while attempt < max_attempts:
            attempt += 1
            outcome = await _update_namespace_operators_once(req)
            outcome.result["attempt"] = attempt
            steps.append(outcome.result)
            _log_job_step(job_id, outcome.result)

            if outcome.succeeded:
                break

            if outcome.meta.get("transient_conflict"):
                last_failure_message = outcome.meta.get("failure_message")
                if attempt < max_attempts:
                    await asyncio.sleep(conflict_backoff)
                    continue
                overall_status = "failed"
                summary_text = (
                    outcome.failure_summary
                    or f"Another operation in progress for namespace {req.namespace}"
                )
                break

            overall_status = "failed"
            summary_text = outcome.failure_summary or summary_text
            last_failure_message = outcome.meta.get("failure_message")
            break
        else:
            overall_status = "failed"
            summary_text = (
                "Unable to update operators"
            )
    except Exception as exc:  # pragma: no cover - defensive
        overall_status = "failed"
        summary_text = "Namespace operators update failed"
        step = {
            "name": "internal_error",
            "command": "<internal>",
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
        }
        steps.append(step)
        _log_job_step(job_id, step)
        logger.exception(
            "Namespace operators job failed",
            extra={"event": "job_failed", "job_id": job_id},
        )
    finally:
        result_payload: Dict[str, Any] = {
            "ok": overall_status == "succeeded",
            "namespace": req.namespace,
            "inputs": {
                "namespace": req.namespace,
                "operators": req.operators.model_dump(),
            },
            "steps": steps,
            "overall_status": overall_status,
            "summary": summary_text,
            "attempts": attempt,
        }
        if last_failure_message:
            result_payload["error_detail"] = last_failure_message

        await jobs.update(
            job_id,
            status=overall_status,
            finished_at=utcnow_iso(),
            summary=summary_text,
            result=result_payload,
        )
        logger.info(
            "job finished",
            extra={
                "event": "job_finished",
                "job_id": job_id,
                "status": overall_status,
                "summary": summary_text,
            },
        )


async def _suspend_user_job(job_id: str, req: SuspendUserRequest) -> None:
    ns = req.namespace or req.username
    await jobs.update(job_id, status="running", started_at=utcnow_iso())
    logger.info(
        "job started",
        extra={"event": "job_started", "job_id": job_id, "username": req.username, "namespace": ns},
    )

    steps: list[Dict[str, Any]] = []
    overall_status = "succeeded"
    summary_bits: list[str] = []
    scale_success = False
    rbac_success = False

    try:
        help_step: Dict[str, Any]
        try:
            help_res = await run_cmd(["everestctl", "accounts", "--help"], timeout=10)
            help_text = (help_res.get("stdout", "") + "\n" + help_res.get("stderr", "")).lower()
            help_step = {**help_res, "name": "accounts_help"}
        except Exception as exc:  # pragma: no cover - defensive
            help_text = ""
            help_step = {
                "name": "accounts_help",
                "command": "everestctl accounts --help",
                "exit_code": 1,
                "stdout": "",
                "stderr": str(exc),
            }
        steps.append(help_step)
        _log_job_step(job_id, help_step)

        supported = set()
        for name in ("deactivate", "disable", "suspend", "lock"):
            if f"\n  {name} " in help_text or f"accounts {name}" in help_text:
                supported.add(name)
        if supported:
            variants = []
            if "deactivate" in supported:
                variants.append(["everestctl", "accounts", "deactivate", "-u", req.username])
            if "disable" in supported:
                variants.append(["everestctl", "accounts", "disable", "-u", req.username])
            if "suspend" in supported:
                variants.append(["everestctl", "accounts", "suspend", "-u", req.username])
            if "lock" in supported:
                variants.append(["everestctl", "accounts", "lock", "-u", req.username])
            for variant in variants:
                res = await run_cmd(variant, timeout=60)
                res.update({"name": "deactivate_account", "variant": variant[2]})
                steps.append(res)
                _log_job_step(job_id, res)
                if res.get("exit_code") == 0:
                    summary_bits.append("account deactivated")
                    break

        if req.scale_statefulsets:
            scale_cmd = build_scale_statefulsets_cmd(ns)
            scale_res = await run_cmd(scale_cmd, timeout=90)
            if scale_res.get("exit_code") != 0:
                errtxt = (scale_res.get("stderr", "") + scale_res.get("stdout", "")).lower()
                if "no objects passed to scale" in errtxt or "no matches for kind \"statefulset\"" in errtxt:
                    scale_res["exit_code"] = 0
                    msg = "no StatefulSets to scale"
                    scale_res["stdout"] = (
                        scale_res.get("stdout", "")
                        + ("\n" if scale_res.get("stdout") else "")
                        + msg
                    ).strip()
            scale_res.update({"name": "scale_down_statefulsets"})
            steps.append(scale_res)
            _log_job_step(job_id, scale_res)
            scale_success = scale_res.get("exit_code") == 0
            if scale_success:
                summary_bits.append("workloads scaled down")
        else:
            scale_success = True

        if req.revoke_rbac:
            rbac_res = await revoke_user_in_rbac_configmap(req.username, timeout=90)
            steps.append(rbac_res)
            _log_job_step(job_id, rbac_res)
            rbac_success = rbac_res.get("exit_code") == 0
            if rbac_success:
                summary_bits.append("RBAC revoked")
        else:
            rbac_success = True

        if not (scale_success or rbac_success):
            overall_status = "failed"
    except Exception as exc:  # pragma: no cover - defensive
        overall_status = "failed"
        step = {
            "name": "internal_error",
            "command": "<internal>",
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
        }
        steps.append(step)
        _log_job_step(job_id, step)
        logger.exception(
            "Suspend user job failed",
            extra={"event": "job_failed", "job_id": job_id},
        )

    summary_text = (
        f"Suspended {req.username} in namespace {ns}" if overall_status == "succeeded" else f"Failed to suspend {req.username}"
    )
    if summary_bits:
        summary_text = summary_text + " (" + ", ".join(summary_bits) + ")"

    result_payload = {
        "ok": overall_status == "succeeded",
        "username": req.username,
        "namespace": ns,
        "inputs": {
            "username": req.username,
            "namespace": ns,
            "scale_statefulsets": req.scale_statefulsets,
            "revoke_rbac": req.revoke_rbac,
        },
        "steps": steps,
        "overall_status": overall_status,
        "summary": summary_text,
    }

    await jobs.update(
        job_id,
        status=overall_status,
        finished_at=utcnow_iso(),
        summary=summary_text,
        result=result_payload,
    )
    logger.info(
        "job finished",
        extra={
            "event": "job_finished",
            "job_id": job_id,
            "status": overall_status,
            "summary": summary_text,
        },
    )


async def _delete_user_job(job_id: str, req: DeleteUserRequest) -> None:
    ns = req.namespace or req.username
    await jobs.update(job_id, status="running", started_at=utcnow_iso())
    logger.info(
        "job started",
        extra={"event": "job_started", "job_id": job_id, "username": req.username, "namespace": ns},
    )

    steps: list[Dict[str, Any]] = []
    overall_status = "succeeded"
    summary_parts: list[str] = []
    namespace_removed = False
    account_deleted = not req.delete_account

    try:
        rm_ns = await run_cmd(["everestctl", "namespaces", "remove", ns], timeout=180)
        rm_ns.update({"name": "remove_namespace"})
        steps.append(rm_ns)
        _log_job_step(job_id, rm_ns)
        if rm_ns.get("exit_code") == 0:
            namespace_removed = True
        else:
            k8s_del = await run_cmd(
                [
                    "kubectl",
                    "delete",
                    "namespace",
                    ns,
                    "--ignore-not-found=true",
                    "--wait=false",
                    "--timeout=30s",
                ],
                timeout=60,
            )
            k8s_del.update({"name": "delete_namespace"})
            steps.append(k8s_del)
            _log_job_step(job_id, k8s_del)
            namespace_removed = k8s_del.get("exit_code") == 0

        rbac_res = await revoke_user_in_rbac_configmap(req.username, timeout=90)
        steps.append(rbac_res)
        _log_job_step(job_id, rbac_res)

        if req.delete_account:
            del_cmds = [
                ["everestctl", "accounts", "delete", "-u", req.username],
                ["everestctl", "accounts", "remove", "-u", req.username],
            ]
            for cmd in del_cmds:
                res = await run_cmd(cmd, timeout=60)
                res.update({"name": "delete_account"})
                steps.append(res)
                _log_job_step(job_id, res)
                if res.get("exit_code") == 0:
                    account_deleted = True
                    break

        if namespace_removed:
            summary_parts.append(f"namespace {ns} removed")
        if account_deleted and req.delete_account:
            summary_parts.append("account deleted")
        if not namespace_removed or not account_deleted:
            overall_status = "failed"
    except Exception as exc:  # pragma: no cover - defensive
        overall_status = "failed"
        step = {
            "name": "internal_error",
            "command": "<internal>",
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
        }
        steps.append(step)
        _log_job_step(job_id, step)
        logger.exception(
            "Delete user job failed",
            extra={"event": "job_failed", "job_id": job_id},
        )

    summary_text = (
        f"Deleted user {req.username} resources" if overall_status == "succeeded" else f"Failed to delete user {req.username}"
    )
    if summary_parts:
        summary_text = summary_text + " (" + ", ".join(summary_parts) + ")"

    result_payload = {
        "ok": overall_status == "succeeded",
        "username": req.username,
        "namespace": ns,
        "inputs": {
            "username": req.username,
            "namespace": ns,
            "delete_account": req.delete_account,
        },
        "steps": steps,
        "overall_status": overall_status,
        "summary": summary_text,
    }

    await jobs.update(
        job_id,
        status=overall_status,
        finished_at=utcnow_iso(),
        summary=summary_text,
        result=result_payload,
    )
    logger.info(
        "job finished",
        extra={
            "event": "job_finished",
            "job_id": job_id,
            "status": overall_status,
            "summary": summary_text,
        },
    )


@app.post(
    "/accounts/password",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin_key)],
)
async def set_account_password(req: PasswordChangeRequest, background: BackgroundTasks):
    job = await jobs.create()
    logger.info(
        "job created",
        extra={"event": "job_created", "job_id": job.job_id, "username": req.username},
    )

    background.add_task(_set_account_password_job, job.job_id, req)
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.post(
    "/namespaces/resources",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin_key)],
)
async def update_namespace_resources(req: NamespaceResourceUpdate, background: BackgroundTasks):
    job = await jobs.create()
    logger.info(
        "job created",
        extra={"event": "job_created", "job_id": job.job_id, "namespace": req.namespace},
    )

    background.add_task(_update_namespace_resources_job, job.job_id, req)
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.post(
    "/namespaces/operators",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin_key)],
)
async def update_namespace_operators(req: NamespaceOperatorsUpdate, background: BackgroundTasks):
    job = await jobs.create()
    logger.info(
        "job created",
        extra={"event": "job_created", "job_id": job.job_id, "namespace": req.namespace},
    )

    background.add_task(_update_namespace_operators_job, job.job_id, req)
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.post(
    "/accounts/suspend",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin_key)],
)
async def suspend_user(req: SuspendUserRequest, background: BackgroundTasks):
    job = await jobs.create()
    ns = req.namespace or req.username
    logger.info(
        "job created",
        extra={
            "event": "job_created",
            "job_id": job.job_id,
            "username": req.username,
            "namespace": ns,
        },
    )

    background.add_task(_suspend_user_job, job.job_id, req)
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.post(
    "/accounts/delete",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin_key)],
)
async def delete_user(req: DeleteUserRequest, background: BackgroundTasks):
    job = await jobs.create()
    ns = req.namespace or req.username
    logger.info(
        "job created",
        extra={
            "event": "job_created",
            "job_id": job.job_id,
            "username": req.username,
            "namespace": ns,
        },
    )

    background.add_task(_delete_user_job, job.job_id, req)
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics exposition."""
    try:
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    except Exception:
        raise HTTPException(status_code=503, detail="prometheus_client not installed")
    data = generate_latest()  # type: ignore
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
