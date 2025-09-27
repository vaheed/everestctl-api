from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from . import execs
from .k8s import build_quota_and_limits_yaml
from .rbac import build_policy_csv, apply_rbac_policy


STATUSES = ("queued", "running", "succeeded", "failed")


@dataclass
class JobStep:
    name: str
    command: Optional[str] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    manifest_preview: Optional[str] = None
    rbac_applied: Optional[bool] = None


@dataclass
class Job:
    job_id: str
    status: str = "queued"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    summary: str = ""
    inputs: Dict[str, Any] = field(default_factory=dict)
    steps: List[JobStep] = field(default_factory=list)
    task: Optional[asyncio.Task] = None

    def to_status_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "result_url": f"/jobs/{self.job_id}/result",
        }

    def to_result_dict(self) -> Dict[str, Any]:
        return {
            "inputs": self.inputs,
            "steps": [asdict(s) for s in self.steps],
            "overall_status": self.status,
            "summary": self.summary,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create_job(self, inputs: Dict[str, Any]) -> Job:
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, inputs=inputs)
        async with self._lock:
            self._jobs[job_id] = job
        return job

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def set(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job


job_store = JobStore()


async def run_bootstrap_job(job: Job) -> None:
    try:
        logger = logging.getLogger("everestctl_api")
        job.status = "running"
        job.started_at = time.time()
        await job_store.set(job)

        username = str(job.inputs["username"]).strip()
        namespace = str(job.inputs.get("namespace") or username).strip()
        operators = job.inputs.get("operators", {}) or {}
        take_ownership = bool(job.inputs.get("take_ownership", False))
        resources = job.inputs.get("resources", {}) or {}
        password = job.inputs.get("password") or os.environ.get("EVEREST_ACCOUNT_PASSWORD") or os.environ.get("DEFAULT_ACCOUNT_PASSWORD")

        mongodb = bool(operators.get("mongodb", False))
        postgresql = bool(operators.get("postgresql", False))
        xtradb = bool(operators.get("xtradb_cluster", False))

        cpu_cores = int(resources.get("cpu_cores", 2))
        ram_mb = int(resources.get("ram_mb", 2048))
        disk_gb = int(resources.get("disk_gb", 20))

        # Preflight
        if not shutil.which("everestctl"):
            job.status = "failed"
            job.summary = "everestctl not found on PATH"
            job.finished_at = time.time()
            await job_store.set(job)
            logger.error(f"job_id={job.job_id} preflight failed: everestctl not found")
            return

        # Preflight diagnostics
        await _preflight_debug(logger)

        ver = await execs.run_cmd_async(["everestctl", "version"])  # prints components version
        logger.info(
            f"job_id={job.job_id} start username={username} namespace={namespace} "
            f"operators={{mongodb:{mongodb},postgresql:{postgresql},xtradb_cluster:{xtradb}}} "
            f"take_ownership={take_ownership} resources={{cpu:{cpu_cores},ram:{ram_mb}Mi,disk:{disk_gb}Gi}} "
            f"everestctl_version_exit={ver.exit_code}"
        )

        # Step 1: create account (long flags per CLI docs; fallback to short flags)
        if not password:
            job.status = "failed"
            job.summary = (
                "Password required for account creation. Provide 'password' in payload or set EVEREST_ACCOUNT_PASSWORD."
            )
            job.finished_at = time.time()
            await job_store.set(job)
            logger.error(f"job_id={job.job_id} missing password for account create")
            return

        create_variants = [
            _everest_cmd(["accounts", "create", "--username", username, "--password", str(password)]),
            _everest_cmd(["accounts", "create", "-u", username, "-p", str(password)]),
            _everest_cmd(["account", "create", "--username", username, "--password", str(password)]),
        ]
        res1, used_cmd1 = await _try_cmd_variants(create_variants, tty=True, timeout=_step_timeout("CREATE_ACCOUNT"))
        step1 = JobStep(
            name="create_account",
            command=_safe_command(used_cmd1),
            exit_code=res1.exit_code,
            stdout=res1.stdout,
            stderr=res1.stderr,
            started_at=res1.started_at,
            finished_at=res1.finished_at,
        )
        job.steps.append(step1)
        logger.info(
            f"job_id={job.job_id} step=create_account exit={res1.exit_code} stderr_tail={_tail(res1.stderr)}"
        )
        if res1.exit_code != 0:
            job.status = "failed"
            if res1.exit_code == 124:
                job.summary = (
                    f"Timeout creating account for {username}. Increase TIMEOUT_CREATE_ACCOUNT or check Everest connectivity."
                )
            else:
                job.summary = f"Failed to create account for {username}"
            job.finished_at = res1.finished_at
            await job_store.set(job)
            logger.error(
                f"job_id={job.job_id} failed create_account command='{step1.command}' exit={res1.exit_code} "
                f"stderr_tail={_tail(res1.stderr)}"
            )
            return

        # Step 2: namespace add
        op_flags_dash = [
            f"--operator.mongodb={'true' if mongodb else 'false'}",
            f"--operator.postgresql={'true' if postgresql else 'false'}",
            f"--operator.xtradb-cluster={'true' if xtradb else 'false'}",
        ]
        op_flags_underscore = [
            f"--operator.mongodb={'true' if mongodb else 'false'}",
            f"--operator.postgresql={'true' if postgresql else 'false'}",
            f"--operator.xtradb_cluster={'true' if xtradb else 'false'}",
        ]
        ownership_variants: list[list[str]] = [[]]
        if take_ownership:
            ownership_variants = [
                ["--take-ownership"],
                ["--owner", username],
                ["--assign-owner", username],
            ]

        ns_variants: list[list[str]] = []
        for subcmd in (["namespaces", "add"], ["namespaces", "create"], ["namespace", "add"]):
            for ops in (op_flags_dash, op_flags_underscore):
                for own in ownership_variants:
                    ns_variants.append(_everest_cmd(list(subcmd) + [namespace] + ops + own))

        res2, used_cmd2 = await _try_cmd_variants(ns_variants, tty=True, timeout=_step_timeout("NAMESPACE_ADD"))
        step2 = JobStep(
            name="add_namespace",
            command=execs.format_command(used_cmd2),
            exit_code=res2.exit_code,
            stdout=res2.stdout,
            stderr=res2.stderr,
            started_at=res2.started_at,
            finished_at=res2.finished_at,
        )
        job.steps.append(step2)
        logger.info(
            f"job_id={job.job_id} step=add_namespace exit={res2.exit_code} stderr_tail={_tail(res2.stderr)}"
        )
        if res2.exit_code != 0:
            job.status = "failed"
            job.summary = f"Failed to add namespace {namespace}"
            job.finished_at = res2.finished_at
            await job_store.set(job)
            logger.error(
                f"job_id={job.job_id} failed add_namespace command='{step2.command}' exit={res2.exit_code} "
                f"stderr_tail={_tail(res2.stderr)}"
            )
            return

        # Step 3: apply resource quota & limit range
        manifest = build_quota_and_limits_yaml(namespace, cpu_cores, ram_mb, disk_gb)
        cmd3 = ["kubectl", "apply", "-n", namespace, "-f", "-"]
        res3 = await execs.run_cmd_async(cmd3, input_text=manifest, timeout=_step_timeout("APPLY_RESOURCES"))
        step3 = JobStep(
            name="apply_resource_quota",
            command=execs.format_command(cmd3),
            exit_code=res3.exit_code,
            stdout=res3.stdout,
            stderr=res3.stderr,
            started_at=res3.started_at,
            finished_at=res3.finished_at,
            manifest_preview=manifest,
        )
        job.steps.append(step3)
        logger.info(
            f"job_id={job.job_id} step=apply_resource_quota exit={res3.exit_code} stderr_tail={_tail(res3.stderr)}"
        )
        if res3.exit_code != 0:
            job.status = "failed"
            job.summary = f"Failed to apply quota/limits to {namespace}"
            job.finished_at = res3.finished_at
            await job_store.set(job)
            logger.error(
                f"job_id={job.job_id} failed apply_resource_quota command='{step3.command}' exit={res3.exit_code} "
                f"stderr_tail={_tail(res3.stderr)}"
            )
            return

        # Step 4: RBAC policy
        policy = build_policy_csv(username, namespace)
        r = apply_rbac_policy(policy)
        step4 = JobStep(
            name="apply_rbac_policy",
            command=r.get("command"),
            exit_code=r.get("exit_code"),
            stdout=r.get("stdout"),
            stderr=r.get("stderr"),
            started_at=r.get("started_at"),
            finished_at=r.get("finished_at"),
            rbac_applied=r.get("rbac_applied"),
        )
        job.steps.append(step4)
        logger.info(
            f"job_id={job.job_id} step=apply_rbac_policy exit={step4.exit_code} "
            f"rbac_applied={step4.rbac_applied} stderr_tail={_tail(step4.stderr or '')}"
        )
        if step4.rbac_applied is False and os.environ.get("EVEREST_RBAC_APPLY_CMD"):
            job.status = "failed"
            job.summary = f"RBAC apply failed for {namespace}"
            job.finished_at = step4.finished_at
            await job_store.set(job)
            logger.error(
                f"job_id={job.job_id} failed apply_rbac_policy command='{step4.command}' exit={step4.exit_code} "
                f"stderr_tail={_tail(step4.stderr or '')}"
            )
            return

        job.status = "succeeded"
        job.summary = (
            f"User {username} and namespace {namespace} created; "
            f"quota applied; role bound."
        )
        job.finished_at = step4.finished_at
        await job_store.set(job)
        logger.info(f"job_id={job.job_id} completed status={job.status}")

    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.summary = f"Unexpected error: {e}"
        await job_store.set(job)
        logging.getLogger("everestctl_api").exception(
            f"job_id={job.job_id} unexpected_error"
        )


def _tail(s: Optional[str], max_chars: int = 400) -> str:
    if not s:
        return ""
    s = s.strip()
    if len(s) <= max_chars:
        return s
    return "…" + s[-max_chars:]


def _everest_cmd(parts: List[str]) -> List[str]:
    base: List[str] = ["everestctl"]
    if os.environ.get("EVERESTCTL_VERBOSE") == "1":
        base.append("-v")
    base.append("--json")
    kube = os.environ.get("KUBECONFIG")
    if kube:
        base += ["-k", kube]
    extra = os.environ.get("EVERESTCTL_EXTRA_ARGS")
    if extra:
        base += extra.split()
    return base + parts


async def _preflight_debug(logger: logging.Logger) -> None:
    kube = os.environ.get("KUBECONFIG", "")
    exists = os.path.exists(kube)
    mode = owner = group = ""
    if exists:
        st = os.stat(kube)
        mode = oct(stat.S_IMODE(st.st_mode))
        owner = str(st.st_uid)
        group = str(st.st_gid)
    logger.info(
        f"preflight kubeconfig path={kube or 'unset'} exists={exists} mode={mode} owner={owner} group={group}"
    )
    which_kubectl = await execs.run_cmd_async(["which", "kubectl"], timeout=5)
    logger.info(
        f"preflight kubectl_path_exit={which_kubectl.exit_code} path={which_kubectl.stdout.strip()}"
    )
    kv = await execs.run_cmd_async(["kubectl", "version", "--client", "--short"], timeout=10)
    logger.info(
        f"preflight kubectl_version_exit={kv.exit_code} stderr_tail={_tail(kv.stderr)}"
    )
    if kv.exit_code != 0:
        kv2 = await execs.run_cmd_async(["kubectl", "version", "--client"], timeout=10)
        logger.info(
            f"preflight kubectl_version_fallback_exit={kv2.exit_code} stdout_tail={_tail(kv2.stdout)} stderr_tail={_tail(kv2.stderr)}"
        )
    kc = await execs.run_cmd_async(["kubectl", "config", "current-context"], timeout=10)
    logger.info(
        f"preflight kubectl_context_exit={kc.exit_code} stdout_tail={_tail(kc.stdout)} stderr_tail={_tail(kc.stderr)}"
    )


def _safe_command(cmd: List[str]) -> str:
    """Return a redacted, shell-quoted command for logs/results.

    Masks values following -p/--password and inline --password=.
    """
    redacted: List[str] = []
    skip_next = False
    for i, tok in enumerate(cmd):
        if skip_next:
            redacted.append("***")
            skip_next = False
            continue
        if tok in ("-p", "--password"):
            redacted.append(tok)
            skip_next = True
            continue
        if tok.startswith("--password="):
            redacted.append("--password=***")
            continue
        redacted.append(tok)
    return execs.format_command(redacted)


async def _try_cmd_variants(
    variants: List[List[str]],
    tty: bool = False,
    timeout: int = 60,
):
    """
    Try a list of command variants, returning the first result and the used cmd.
    If a variant fails with an 'unknown command/flag' style error, try the next.
    Otherwise, return that failure.
    """
    last_res = None
    for cmd in variants:
        res = await (execs.run_cmd_tty_async(cmd, timeout=timeout) if tty else execs.run_cmd_async(cmd, timeout=timeout))
        if res.exit_code == 0:
            return res, cmd
        stderr = (res.stderr or "").lower()
        if "unknown command" in stderr or "unknown flag" in stderr or "unknown shorthand flag" in stderr:
            last_res = res
            continue
        return res, cmd
    # If all variants failed with unknown errors, return the last
    return last_res or await execs.run_cmd_async(variants[-1], timeout=timeout), variants[-1]


def _step_timeout(step: str) -> int:
    """Resolve per-step timeouts from env with sensible defaults.

    STEP envs:
      - TIMEOUT_CREATE_ACCOUNT
      - TIMEOUT_NAMESPACE_ADD
      - TIMEOUT_APPLY_RESOURCES
    Fallback to SUBPROCESS_TIMEOUT or 60 seconds.
    """
    mapping = {
        "CREATE_ACCOUNT": "TIMEOUT_CREATE_ACCOUNT",
        "NAMESPACE_ADD": "TIMEOUT_NAMESPACE_ADD",
        "APPLY_RESOURCES": "TIMEOUT_APPLY_RESOURCES",
    }
    key = mapping.get(step.upper(), "")
    default = int(os.environ.get("SUBPROCESS_TIMEOUT", "60"))
    if key and os.environ.get(key):
        try:
            return int(os.environ[key])
        except ValueError:
            return default
    return default
