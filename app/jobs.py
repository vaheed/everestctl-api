from __future__ import annotations

import asyncio
import os
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
        job.status = "running"
        await job_store.set(job)

        username = str(job.inputs["username"]).strip()
        namespace = str(job.inputs.get("namespace") or username).strip()
        operators = job.inputs.get("operators", {}) or {}
        take_ownership = bool(job.inputs.get("take_ownership", False))
        resources = job.inputs.get("resources", {}) or {}

        mongodb = bool(operators.get("mongodb", False))
        postgresql = bool(operators.get("postgresql", False))
        xtradb = bool(operators.get("xtradb_cluster", False))

        cpu_cores = int(resources.get("cpu_cores", 2))
        ram_mb = int(resources.get("ram_mb", 2048))
        disk_gb = int(resources.get("disk_gb", 20))

        # Step 1: create account
        cmd1 = ["everestctl", "accounts", "create", "-u", username]
        res1 = execs.run_cmd(cmd1)
        step1 = JobStep(
            name="create_account",
            command=execs.format_command(cmd1),
            exit_code=res1.exit_code,
            stdout=res1.stdout,
            stderr=res1.stderr,
            started_at=res1.started_at,
            finished_at=res1.finished_at,
        )
        job.steps.append(step1)
        if res1.exit_code != 0:
            job.status = "failed"
            job.summary = f"Failed to create account for {username}"
            job.finished_at = res1.finished_at
            await job_store.set(job)
            return

        # Step 2: namespace add
        cmd2 = [
            "everestctl",
            "namespaces",
            "add",
            namespace,
            f"--operator.mongodb={'true' if mongodb else 'false'}",
            f"--operator.postgresql={'true' if postgresql else 'false'}",
            f"--operator.xtradb-cluster={'true' if xtradb else 'false'}",
        ]
        if take_ownership:
            cmd2.append("--take-ownership")
        res2 = execs.run_cmd(cmd2)
        step2 = JobStep(
            name="add_namespace",
            command=execs.format_command(cmd2),
            exit_code=res2.exit_code,
            stdout=res2.stdout,
            stderr=res2.stderr,
            started_at=res2.started_at,
            finished_at=res2.finished_at,
        )
        job.steps.append(step2)
        if res2.exit_code != 0:
            job.status = "failed"
            job.summary = f"Failed to add namespace {namespace}"
            job.finished_at = res2.finished_at
            await job_store.set(job)
            return

        # Step 3: apply resource quota & limit range
        manifest = build_quota_and_limits_yaml(namespace, cpu_cores, ram_mb, disk_gb)
        cmd3 = ["kubectl", "apply", "-n", namespace, "-f", "-"]
        res3 = execs.run_cmd(cmd3, input_text=manifest)
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
        if res3.exit_code != 0:
            job.status = "failed"
            job.summary = f"Failed to apply quota/limits to {namespace}"
            job.finished_at = res3.finished_at
            await job_store.set(job)
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
        if step4.rbac_applied is False and os.environ.get("EVEREST_RBAC_APPLY_CMD"):
            job.status = "failed"
            job.summary = f"RBAC apply failed for {namespace}"
            job.finished_at = step4.finished_at
            await job_store.set(job)
            return

        job.status = "succeeded"
        job.summary = (
            f"User {username} and namespace {namespace} created; "
            f"quota applied; role bound."
        )
        job.finished_at = step4.finished_at
        await job_store.set(job)

    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.summary = f"Unexpected error: {e}"
        await job_store.set(job)
