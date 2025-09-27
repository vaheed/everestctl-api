from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import execs
from .jobs import job_store, run_bootstrap_job
from .parsers import try_parse_json_or_table


ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "changeme")


def get_logger() -> logging.Logger:
    logger = logging.getLogger("everestctl_api")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt='{"ts": %(asctime)s, "level": "%(levelname)s", "msg": "%(message)s", "logger": "%(name)s"}',
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


logger = get_logger()

app = FastAPI(title="Everestctl Async API", version="1.0.0")


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = req_id
    start = time.time()
    try:
        response: Response = await call_next(request)
    finally:
        dur = time.time() - start
        logger.info(
            f"request {request.method} {request.url.path} id={req_id} dur={dur:.3f}s",
        )
    response.headers["X-Request-ID"] = req_id
    return response


async def require_admin_key(x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key")):
    if not x_admin_key or x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: missing or invalid X-Admin-Key")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    # Could add additional checks here
    return {"status": "ready"}


@app.post("/bootstrap/users", status_code=202, dependencies=[Depends(require_admin_key)])
async def submit_bootstrap_job(payload: Dict[str, Any]):
    username = payload.get("username")
    if not username or not str(username).strip():
        raise HTTPException(status_code=422, detail="username is required")

    inputs = {
        "username": str(username).strip(),
        "namespace": (payload.get("namespace") or str(username).strip()),
        "operators": payload.get("operators") or {},
        "take_ownership": bool(payload.get("take_ownership", False)),
        "resources": payload.get("resources") or {},
    }

    job = await job_store.create_job(inputs)

    async def runner():
        await run_bootstrap_job(job)

    asyncio.create_task(runner())
    return {"job_id": job.job_id, "status_url": f"/jobs/{job.job_id}"}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_admin_key)])
async def get_job_status(job_id: str):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_status_dict()


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_admin_key)])
async def get_job_result(job_id: str):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("succeeded", "failed"):
        raise HTTPException(status_code=409, detail="job not completed yet")
    return job.to_result_dict()


@app.get("/accounts/list", dependencies=[Depends(require_admin_key)])
async def accounts_list():
    # Intentionally using singular per instruction: `everestctl account list`
    cmd = ["everestctl", "--json", "account", "list"]
    res = await execs.run_cmd_async(cmd)  # type: ignore[arg-type]
    logger.info(
        f"request accounts_list exit={res.exit_code} stderr_tail={(res.stderr or '').strip()[-200:]}"
    )
    if res.exit_code != 0:
        # Provide stderr tail
        detail = res.stderr[-500:] if res.stderr else ""
        raise HTTPException(status_code=502, detail={"error": "everestctl failed", "detail": detail})

    parsed = try_parse_json_or_table(res.stdout)
    return parsed
