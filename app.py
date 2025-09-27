import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

import cli
from auth import IdempotencyStore, idempotency_dependency, make_rate_limiter, require_admin_key
from crd import ensure_crd_applied, upsert_tenant_resource_policy
from db import Database
from logging_setup import RequestContextMiddleware, setup_logging
from quotas import enforce_cluster_create, enforce_db_user_create
from rbac import append_policy_lines, validate_policy_lines
from schemas import (
    AccountsCreate,
    AccountsDelete,
    AccountsSetPassword,
    BootstrapTenantRequest,
    EnforceClusterCreateRequest,
    LimitsUpsertRequest,
    NamespacesAddRequest,
    NamespacesRemoveRequest,
    NamespacesUpdateRequest,
    RBACAppendRequest,
    RBACCanRequest,
    RotatePasswordRequest,
    UsageRegisterClusterRequest,
    UsageRegisterDbUserRequest,
)


ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
POLICY_FILE = os.environ.get("POLICY_FILE", "/var/lib/everest/policy/policy.csv")
APPLY_RBAC = os.environ.get("APPLY_RBAC", "true").lower() == "true"
SQLITE_DB = os.environ.get("SQLITE_DB", "/var/lib/everest/data/tenant_proxy.db")
RATE_QPS = float(os.environ.get("RATE_QPS", "10"))
RATE_BURST = int(os.environ.get("RATE_BURST", "20"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "false").lower() == "true"

setup_logging(LOG_LEVEL)
logger = logging.getLogger("everestctl_api")

app = FastAPI(title="Everest Tenant Bootstrap Proxy", version="0.1.0")
app.add_middleware(RequestContextMiddleware)

db = Database(SQLITE_DB)


# Metrics (register only when enabled to avoid duplicate registration on reload during tests)
if METRICS_ENABLED:
    http_requests_total = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
    http_latency_seconds = Histogram("http_latency_seconds", "HTTP request latency", ["method", "path"])  # type: ignore[assignment]
    cli_invocations_total = Counter("cli_invocations_total", "CLI invocations", ["cmd", "exit_code"])  # used via logging if desired
    quota_violations_total = Counter("quota_violations_total", "Quota violations", ["type"])  # type: ignore[assignment]
    rate_limit_block_total = Counter("rate_limit_block_total", "Rate limit blocks")
else:
    http_requests_total = None
    http_latency_seconds = None
    cli_invocations_total = None
    quota_violations_total = None
    rate_limit_block_total = None


def metrics_enabled():
    return METRICS_ENABLED


# Dependencies
require_key_dep = require_admin_key(ADMIN_API_KEY)
rate_limit_dep = make_rate_limiter(RATE_QPS, RATE_BURST)
idempo_store = IdempotencyStore(
    put_fn=lambda key, body, status: db.idempotency_put(key, "application/json", status, json.loads(body)),
    get_fn=db.idempotency_get,
)
idempotency_dep = idempotency_dependency(idempo_store)


@app.on_event("startup")
def startup_event():
    logger.info("starting up")
    # Verify everestctl available and supports expected commands unless skipped
    if not cli.verify_commands():
        # Fail fast on startup
        raise RuntimeError("everestctl verify failed")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(_: Any = Depends(require_key_dep)):
    # everestctl version
    try:
        code, out, err = cli.get_version()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"everestctl version failed: {e}")
    if code != 0:
        raise HTTPException(status_code=503, detail=f"everestctl version error: {err}")
    # DB & policy file access
    try:
        # Simple write test
        db.list_tenants()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db error: {e}")
    try:
        os.makedirs(os.path.dirname(POLICY_FILE), exist_ok=True)
        open(POLICY_FILE, "a").close()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"policy file error: {e}")
    return {"status": "ready", "everestctl_version": out.strip()}


@app.post("/bootstrap/tenant")
def bootstrap_tenant(
    req: BootstrapTenantRequest,
    _: Any = Depends(require_key_dep),
    __: Any = Depends(rate_limit_dep),
    key: Optional[str] = Depends(idempotency_dep),
):
    ns = req.namespace
    user = req.username
    # 1) namespace add
    code, out, err = cli.namespaces_add(
        ns,
        {
            "postgresql": bool(req.operators.postgresql) if req.operators.postgresql is not None else False,
            "mongodb": bool(req.operators.mongodb) if req.operators.mongodb is not None else False,
            "xtradb_cluster": bool(req.operators.xtradb_cluster) if req.operators.xtradb_cluster is not None else False,
        },
        take_ownership=False,
    )
    if code != 0 and "already exists" not in (out + err).lower():
        db.write_audit("admin", "namespaces_add", ns, user, None, 500, "namespaces add", code, out, err)
        raise HTTPException(status_code=500, detail=f"namespaces add failed: {err}")
    # 2) accounts create (idempotent)
    code, out, err = cli.accounts_create(user)
    if code != 0 and "already exists" not in (out + err).lower():
        db.write_audit("admin", "accounts_create", ns, user, None, 500, "accounts create", code, out, err)
        raise HTTPException(status_code=500, detail=f"account create failed: {err}")
    # 3) set password
    code, out, err = cli.accounts_set_password(user, req.password)
    if code != 0:
        db.write_audit("admin", "accounts_set_password", ns, user, None, 500, "accounts set-password", code, out, err)
        raise HTTPException(status_code=500, detail=f"set password failed: {err}")
    # 4) RBAC append and validate
    role = f"role:tenant-{ns}"
    lines = [
        f"p, {role}, namespaces, read, {ns}",
        f"p, {role}, database-engines, read, {ns}/*",
        f"p, {role}, database-clusters, *, {ns}/*",
        f"p, {role}, database-cluster-backups, *, {ns}/*",
        f"p, {role}, database-cluster-restores, *, {ns}/*",
        f"p, {role}, database-cluster-credentials, read, {ns}/*",
        f"p, {role}, backup-storages, *, {ns}/*",
        f"p, {role}, monitoring-instances, *, {ns}/*",
        f"g, {user}, {role}",
    ]
    validate_policy_lines(lines)
    append_policy_lines(POLICY_FILE, lines)
    if APPLY_RBAC:
        vcode, vout, verr = cli.rbac_validate(POLICY_FILE)
        if vcode != 0:
            db.write_audit("admin", "rbac_validate", ns, user, None, 500, "rbac validate", vcode, vout, verr)
            raise HTTPException(status_code=500, detail=f"rbac validate failed: {verr}")
    # 5) Create or update TenantResourcePolicy CR
    limits = {
        "namespace": ns,
        "max_clusters": 3,
        "allowed_engines": ["postgresql", "mysql"],
        "cpu_limit_cores": 4.0,
        "memory_limit_bytes": 17179869184,
        "max_db_users": 20,
    }
    try:
        upsert_tenant_resource_policy(ns, limits, limits["allowed_engines"])  # best-effort
    except Exception as e:
        logger.warning("crd upsert failed: %s", e)
    # 6) Initialize DB rows
    db.upsert_limits(ns, limits)
    db.init_usage_if_missing(ns)

    result = {"status": "ok", "namespace": ns, "username": user}
    if key:
        db.idempotency_put(key, "application/json", 200, result)
    return result


# Safe CLI wrappers
@app.post("/cli/accounts/create")
def api_accounts_create(req: AccountsCreate, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.accounts_create(req.username)
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


@app.post("/cli/accounts/set-password")
def api_accounts_set_password(req: AccountsSetPassword, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.accounts_set_password(req.username, req.new_password)
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


@app.get("/cli/accounts/list")
def api_accounts_list(_: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.accounts_list()
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


@app.delete("/cli/accounts")
def api_accounts_delete(req: AccountsDelete, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.accounts_delete(req.username)
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


@app.post("/cli/namespaces/add")
def api_namespaces_add(req: NamespacesAddRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.namespaces_add(
        req.namespace,
        {
            "postgresql": bool(req.operators.postgresql) if req.operators.postgresql is not None else False,
            "mongodb": bool(req.operators.mongodb) if req.operators.mongodb is not None else False,
            "xtradb_cluster": bool(req.operators.xtradb_cluster) if req.operators.xtradb_cluster is not None else False,
        },
        req.take_ownership,
    )
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


@app.post("/cli/namespaces/update")
def api_namespaces_update(req: NamespacesUpdateRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.namespaces_update(req.namespace)
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


@app.delete("/cli/namespaces/remove")
def api_namespaces_remove(req: NamespacesRemoveRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.namespaces_remove(req.namespace, req.keep_namespace)
    return JSONResponse(status_code=200 if code == 0 else 500, content={"exit_code": code, "stdout": out, "stderr": err})


# RBAC admin
@app.post("/rbac/append")
def api_rbac_append(req: RBACAppendRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    validate_policy_lines(req.lines)
    path, count = append_policy_lines(POLICY_FILE, req.lines)
    if APPLY_RBAC:
        code, out, err = cli.rbac_validate(POLICY_FILE)
        if code != 0:
            raise HTTPException(status_code=500, detail=f"rbac validate failed: {err}")
    return {"path": path, "lines_appended": count}


@app.post("/rbac/can")
def api_rbac_can(req: RBACCanRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.rbac_can(POLICY_FILE, req.user, req.resource, req.verb, req.object)
    status = 200 if code == 0 else 403
    return JSONResponse(status_code=status, content={"exit_code": code, "stdout": out, "stderr": err})


# Limits / usage
@app.put("/limits")
def api_limits_upsert(req: LimitsUpsertRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    limits = req.model_dump()
    db.upsert_limits(req.namespace, limits)
    try:
        upsert_tenant_resource_policy(req.namespace, limits, [str(e) for e in req.allowed_engines])
    except Exception as e:
        logger.warning("crd upsert failed: %s", e)
    return {"status": "ok"}


@app.get("/limits/{namespace}")
def api_limits_get(namespace: str, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    row = db.get_limits(namespace)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.post("/enforce/cluster-create")
def api_enforce_cluster_create(req: EnforceClusterCreateRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    ok, reason = enforce_cluster_create(db, req.namespace, str(req.engine), req.cpu_request_cores, req.memory_request_bytes)
    if not ok:
        quota_violations_total.labels(type="cluster").inc() if metrics_enabled() else None
        raise HTTPException(status_code=403, detail=reason)
    return {"status": "ok"}


@app.post("/usage/register-cluster")
def api_usage_register_cluster(req: UsageRegisterClusterRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    try:
        db.apply_cluster_delta(req.namespace, str(req.op), req.cpu_cores, req.memory_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "usage": db.get_usage(req.namespace)}


@app.post("/usage/register-db-user")
def api_usage_register_db_user(req: UsageRegisterDbUserRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    if str(req.op) == "create":
        ok, reason = enforce_db_user_create(db, req.namespace)
        if not ok:
            quota_violations_total.labels(type="db_user").inc() if metrics_enabled() else None
            raise HTTPException(status_code=403, detail=reason)
    try:
        db.apply_db_user_delta(req.namespace, str(req.op))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "usage": db.get_usage(req.namespace)}


# Day-2 ops (wrappers around CLI)
@app.delete("/users")
def api_delete_user(req: AccountsDelete, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.accounts_delete(req.username)
    status = 200 if code == 0 else 500
    return JSONResponse(status_code=status, content={"exit_code": code, "stdout": out, "stderr": err})


@app.delete("/namespaces")
def api_delete_namespace(req: NamespacesRemoveRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.namespaces_remove(req.namespace, keep_namespace=req.keep_namespace)
    status = 200 if code == 0 else 500
    return JSONResponse(status_code=status, content={"exit_code": code, "stdout": out, "stderr": err})


@app.post("/users/rotate-password")
def api_rotate_password(req: RotatePasswordRequest, _: Any = Depends(require_key_dep), __: Any = Depends(rate_limit_dep)):
    code, out, err = cli.accounts_set_password(req.username, req.new_password)
    status = 200 if code == 0 else 500
    return JSONResponse(status_code=status, content={"exit_code": code, "stdout": out, "stderr": err})


# Admin views
@app.get("/tenants")
def api_tenants(_: Any = Depends(require_key_dep)):
    return db.list_tenants()


@app.get("/audit")
def api_audit(_: Any = Depends(require_key_dep)):
    # For brevity, return last N rows
    import sqlite3

    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    rows = cur.execute("SELECT ts,actor,action,namespace,username,response_code FROM audit ORDER BY ts DESC LIMIT 200").fetchall()
    return [{"ts": r[0], "actor": r[1], "action": r[2], "namespace": r[3], "username": r[4], "response_code": r[5]} for r in rows]


# Raw lists (cache omitted for brevity)
@app.get("/users/raw")
def api_users_raw(_: Any = Depends(require_key_dep)):
    code, out, err = cli.accounts_list()
    return PlainTextResponse(out if code == 0 else err, status_code=200 if code == 0 else 500)


@app.get("/namespaces/raw")
def api_namespaces_raw(_: Any = Depends(require_key_dep)):
    code, out, err = cli.run(["everestctl", "namespaces", "list"])  # type: ignore[arg-type]
    return PlainTextResponse(out if code == 0 else err, status_code=200 if code == 0 else 500)


@app.get("/metrics")
def metrics():
    if not METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="metrics disabled")
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
