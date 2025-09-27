#!/usr/bin/env python3
import os
import sys
import json
import hmac
import hashlib
import asyncio
import time
import shutil
import tempfile
import csv
import sqlite3
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional, List, Dict, Any
import contextlib
import re

from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pydantic import field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool

from filelock import FileLock, Timeout as FileLockTimeout
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest

# -------------------- Configuration --------------------

class Settings(BaseModel):
    API_KEY: str = Field(default_factory=lambda: os.getenv("API_KEY", ""))
    EVERESTCTL_PATH: str = Field(default_factory=lambda: os.getenv("EVERESTCTL_PATH", "/usr/local/bin/everestctl"))
    RBAC_POLICY_PATH: str = Field(default_factory=lambda: os.getenv("RBAC_POLICY_PATH", "/data/policy.csv"))
    DB_PATH: str = Field(default_factory=lambda: os.getenv("DB_PATH", "/data/audit.db"))
    DB_URL: str = Field(default_factory=lambda: os.getenv("DB_URL", ""))
    RATE_LIMIT_PER_MIN: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MIN", "120")))
    REQUEST_TIMEOUT_SEC: int = Field(default_factory=lambda: int(os.getenv("REQUEST_TIMEOUT_SEC", "20")))
    REQUEST_RETRIES: int = Field(default_factory=lambda: int(os.getenv("REQUEST_RETRIES", "2")))
    ALLOWED_ENGINES: str = Field(default_factory=lambda: os.getenv("ALLOWED_ENGINES", "postgres,mysql"))
    MAX_CLUSTERS_PER_TENANT: int = Field(default_factory=lambda: int(os.getenv("MAX_CLUSTERS_PER_TENANT", "5")))
    LOG_LEVEL: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    METRICS_ENABLED: bool = Field(default_factory=lambda: os.getenv("METRICS_ENABLED", "true").lower() == "true")
    CORS_ALLOW_ORIGINS: str = Field(default_factory=lambda: os.getenv("CORS_ALLOW_ORIGINS", "*"))
    HEALTH_STARTUP_PROBE_CMD: str = Field(default_factory=lambda: os.getenv("HEALTH_STARTUP_PROBE_CMD", "version"))
    # Rate limiter burst (per minute window)
    RATE_LIMIT_BURST: int = Field(default_factory=lambda: int(os.getenv("RATE_LIMIT_BURST", "150")))
    # Enable if using SSO; skips local user creation/password ops
    SSO_ENABLED: bool = Field(default_factory=lambda: os.getenv("SSO_ENABLED", "false").lower() == "true")
    # Validate RBAC via everestctl `settings rbac validate` after changes
    RBAC_VALIDATE_ON_CHANGE: bool = Field(default_factory=lambda: os.getenv("RBAC_VALIDATE_ON_CHANGE", "true").lower() == "true")
    # Some everestctl subcommands require a TTY; emulate with PTY if needed
    CLI_FORCE_PTY: bool = Field(default_factory=lambda: os.getenv("CLI_FORCE_PTY", "true").lower() == "true")

    @field_validator("EVERESTCTL_PATH")
    @classmethod
    def normalize_cli_path(cls, v: str) -> str:
        return v.strip()

@lru_cache
def get_settings() -> Settings:
    return Settings()

# -------------------- Logging --------------------

class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "event"):
            payload["event"] = record.event
        if hasattr(record, "extras"):
            payload.update(record.extras)
        return json.dumps(payload)

logger = logging.getLogger("everestctl_api")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonLogFormatter())
logger.addHandler(handler)
# Set default log level; refined on startup
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# -------------------- Metrics --------------------
registry = CollectorRegistry()
http_requests = Counter("http_requests_total", "HTTP requests", ["method", "path", "code"], registry=registry)
http_latency = Histogram("http_request_latency_seconds", "HTTP request latency", ["method", "path"], registry=registry)
cli_runs = Counter("cli_runs_total", "CLI runs", ["cmd", "status"], registry=registry)
quota_violations = Counter("quota_violations_total", "Quota violations", ["type"], registry=registry)
inflight = Gauge("inflight_requests", "Inflight requests", registry=registry)

# -------------------- Rate Limiter --------------------

class RateLimiter:
    def __init__(self, per_min: int, burst: int):
        self.per_min = per_min
        self.burst = burst
        self.state: Dict[str, Dict[str, float]] = {}
        self.lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        async with self.lock:
            now = time.monotonic()
            bucket = self.state.get(key, {"tokens": float(self.burst), "ts": now})
            # refill
            elapsed = now - bucket["ts"]
            refill = (self.per_min / 60.0) * elapsed
            bucket["tokens"] = min(self.burst, bucket["tokens"] + refill)
            bucket["ts"] = now
            if bucket["tokens"] >= 1.0:
                bucket["tokens"] -= 1.0
                self.state[key] = bucket
                return True
            self.state[key] = bucket
            return False

# -------------------- Database (Audit + Counters) --------------------

def init_db(db_path: str):
    db_url = os.getenv("DB_URL", "").strip()
    if db_url:
        # Postgres (via psycopg2-binary)
        try:
            import psycopg2  # type: ignore
        except Exception as e:
            logger.error("", extra={"event":"db_init_fail","extras":{"reason":"psycopg2 missing","detail":str(e)}})
            raise
        with contextlib.closing(psycopg2.connect(db_url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_log(
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target TEXT NOT NULL,
                        details TEXT NOT NULL
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS counters(
                        tenant TEXT PRIMARY KEY,
                        clusters INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
    else:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                details TEXT NOT NULL
            );
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS counters(
                tenant TEXT PRIMARY KEY,
                clusters INTEGER NOT NULL DEFAULT 0
            );
            """)
            conn.commit()

def audit(db_path: str, actor: str, action: str, target: str, details: Dict[str, Any]):
    db_url = os.getenv("DB_URL", "").strip()
    ts = datetime.now(timezone.utc).isoformat()
    if db_url:
        import psycopg2  # type: ignore
        with contextlib.closing(psycopg2.connect(db_url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log(ts, actor, action, target, details) VALUES (%s, %s, %s, %s, %s);",
                    (ts, actor, action, target, json.dumps(details)),
                )
    else:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO audit_log(ts, actor, action, target, details) VALUES (?, ?, ?, ?, ?);",
                (ts, actor, action, target, json.dumps(details))
            )
            conn.commit()

def get_counter(db_path: str, tenant: str) -> int:
    db_url = os.getenv("DB_URL", "").strip()
    if db_url:
        import psycopg2  # type: ignore
        with contextlib.closing(psycopg2.connect(db_url)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT clusters FROM counters WHERE tenant=%s;", (tenant,))
                row = cur.fetchone()
                return int(row[0]) if row else 0
    else:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("SELECT clusters FROM counters WHERE tenant=?;", (tenant,))
            row = cur.fetchone()
            return int(row[0]) if row else 0

def inc_counter(db_path: str, tenant: str, delta: int) -> int:
    db_url = os.getenv("DB_URL", "").strip()
    if db_url:
        import psycopg2  # type: ignore
        with contextlib.closing(psycopg2.connect(db_url)) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT clusters FROM counters WHERE tenant=%s;", (tenant,))
                row = cur.fetchone()
                if row:
                    new_val = max(0, int(row[0]) + delta)
                    cur.execute("UPDATE counters SET clusters=%s WHERE tenant=%s;", (new_val, tenant))
                else:
                    new_val = max(0, delta)
                    cur.execute("INSERT INTO counters(tenant, clusters) VALUES(%s, %s);", (tenant, new_val))
                return new_val
    else:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("SELECT clusters FROM counters WHERE tenant=?;", (tenant,))
            row = cur.fetchone()
            if row:
                new_val = max(0, int(row[0]) + delta)
                conn.execute("UPDATE counters SET clusters=? WHERE tenant=?;", (new_val, tenant))
            else:
                new_val = max(0, delta)
                conn.execute("INSERT INTO counters(tenant, clusters) VALUES(?, ?);", (tenant, new_val))
            conn.commit()
            return new_val

# -------------------- Security --------------------

def const_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

async def api_key_auth(request: Request, settings: Settings = Depends(get_settings)):
    key = request.headers.get("x-api-key") or ""
    if not const_time_compare(key, settings.API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

# -------------------- Middleware --------------------

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        method = request.method
        path = request.url.path
        start = time.perf_counter()
        inflight.inc()
        try:
            response = await call_next(request)
            code = response.status_code
            duration = time.perf_counter() - start
            http_requests.labels(method, path, str(code)).inc()
            http_latency.labels(method, path).observe(duration)
            logger.info("", extra={"event": "http_request", "extras": {"method": method, "path": path, "code": code, "duration_s": round(duration, 4)}})
            return response
        finally:
            inflight.dec()

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        client_key = request.headers.get("x-api-key") or request.client.host
        if not await self.limiter.allow(client_key):
            quota_violations.labels("rate_limit").inc()
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        return await call_next(request)

# -------------------- Models --------------------

class TenantRequest(BaseModel):
    user: str
    namespace: str
    password: str
    engine: str = Field(..., description="Database engine, must be allowed")
    operators: Optional[List[str]] = Field(default=None, description="Optional DB operators to enable for the namespace")

class RotatePasswordRequest(BaseModel):
    user: str
    new_password: str

class DeleteTenantRequest(BaseModel):
    user: str
    namespace: str

class UserOnlyRequest(BaseModel):
    user: str

class CreateUserRequest(BaseModel):
    user: str
    password: Optional[str] = None
    enabled: Optional[bool] = True

# -------------------- RBAC Policy Helpers --------------------

def read_policy(path: str) -> List[List[str]]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.reader(f))

def write_policy_atomic(path: str, rows: List[List[str]]):
    dirn = os.path.dirname(path) or "."
    os.makedirs(dirn, exist_ok=True)
    backup = f"{path}.{int(time.time())}.bak"
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dirn, prefix=".policy.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", newline="") as tmp:
            writer = csv.writer(tmp)
            writer.writerows(rows)
        if os.path.exists(path):
            shutil.copy2(path, backup)
        os.replace(tmp_path, path)  # atomic
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def rbac_add(path: str, user: str, namespace: str):
    try:
        with FileLock(path + ".lock", timeout=10):
            rows = read_policy(path)
            entry = ["p", user, namespace, "write"]
            if entry not in rows:
                rows.append(entry)
                write_policy_atomic(path, rows)
    except FileLockTimeout:
        raise HTTPException(503, "policy lock timeout")

def rbac_remove(path: str, user: str, namespace: str):
    try:
        with FileLock(path + ".lock", timeout=10):
            rows = read_policy(path)
            new_rows = [
                r
                for r in rows
                if not (len(r) >= 4 and r[0] == "p" and r[1] == user and r[2] == namespace)
            ]
            write_policy_atomic(path, new_rows)
    except FileLockTimeout:
        raise HTTPException(503, "policy lock timeout")

async def rbac_write_and_validate(path: str, new_rows: List[List[str]], validator):
    """Write policy rows atomically then run validator(); on failure, roll back to original content."""
    original_rows = read_policy(path)
    try:
        write_policy_atomic(path, new_rows)
        await validator()
    except Exception:
        # Roll back to original rows and re-raise
        write_policy_atomic(path, original_rows)
        raise

async def rbac_add_validate_if_enabled(settings: Settings, user: str, namespace: str):
    path = settings.RBAC_POLICY_PATH
    if not settings.RBAC_VALIDATE_ON_CHANGE:
        return rbac_add(path, user, namespace)
    try:
        with FileLock(path + ".lock", timeout=10):
            rows = read_policy(path)
            entry = ["p", user, namespace, "write"]
            if entry in rows:
                return
            rows_with = rows + [entry]
            async def _validate():
                await run_cli(settings, ["settings", "rbac", "validate"])
            await rbac_write_and_validate(path, rows_with, _validate)
    except FileLockTimeout:
        raise HTTPException(503, "policy lock timeout")

async def rbac_remove_validate_if_enabled(settings: Settings, user: str, namespace: str):
    path = settings.RBAC_POLICY_PATH
    if not settings.RBAC_VALIDATE_ON_CHANGE:
        return rbac_remove(path, user, namespace)
    try:
        with FileLock(path + ".lock", timeout=10):
            rows = read_policy(path)
            filtered = [r for r in rows if not (len(r) >= 4 and r[0] == "p" and r[1] == user and r[2] == namespace)]
            async def _validate():
                await run_cli(settings, ["settings", "rbac", "validate"])
            await rbac_write_and_validate(path, filtered, _validate)
    except FileLockTimeout:
        raise HTTPException(503, "policy lock timeout")

# -------------------- CLI Wrapper --------------------

def _mask_secrets(args: List[str]) -> List[str]:
    masked: List[str] = []
    secret_keys = {"password", "new_password", "api_key", "token"}
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            if k.lower() in secret_keys:
                masked.append(f"{k}=****")
                continue
        masked.append(a)
    return masked

async def run_cli(settings: Settings, args: List[str]) -> Dict[str, Any]:
    """Safely run everestctl with validated args and timeouts/retries."""
    cmd = [settings.EVERESTCTL_PATH] + args
    # Validate: only allow safe characters in args (letters, digits, dashes, underscores, dots, slashes, equals, colon, comma)
    for a in cmd:
        if not all(c.isalnum() or c in "-_./=:," for c in a):
            raise HTTPException(400, f"invalid character in arg: {a}")
    last_exc = None
    for attempt in range(settings.REQUEST_RETRIES + 1):
        try:
            if settings.CLI_FORCE_PTY:
                # Emulate a TTY using a PTY so everestctl won't try to open /dev/tty
                rc, out, err = await _run_with_pty(cmd, settings.REQUEST_TIMEOUT_SEC)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.REQUEST_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    proc.kill()
                    stdout, stderr = await proc.communicate()
                    raise TimeoutError("everestctl timed out")
                rc = proc.returncode
                out = stdout.decode(errors="replace").strip()
                err = stderr.decode(errors="replace").strip()
            cli_runs.labels(" ".join(args[:2]), "ok" if rc==0 else "error").inc()
            logger.info(
                "",
                extra={"event": "cli_run", "extras": {"args": _mask_secrets(args), "rc": rc}},
            )
            if rc != 0:
                last_exc = RuntimeError(f"CLI error rc={rc}: {err or out}")
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
                continue
            # Try parse JSON if looks like JSON; otherwise return raw
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError:
                parsed = {"raw": out}
            return {"rc": rc, "stdout": parsed, "stderr": err}
        except Exception as e:
            last_exc = e
            await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
    raise HTTPException(502, f"CLI failed after retries: {last_exc}")

async def _run_with_pty(cmd: List[str], timeout: int) -> (int, str, str):
    """Run a command attached to a PTY and capture combined output.
    Returns (rc, stdout, stderr_text_placeholder) where stderr is folded into stdout.
    """
    import os, signal
    master_fd, slave_fd = os.openpty()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)
        buf = bytearray()

        async def reader():
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)

        rtask = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            rtask.cancel()
            raise TimeoutError("everestctl timed out")
        # Allow small drain
        await asyncio.sleep(0.05)
        rtask.cancel()
        out = buf.decode(errors="replace").strip()
        return proc.returncode, out, ""
    finally:
        try:
            os.close(master_fd)
        except Exception:
            pass

async def run_cli_idempotent(settings: Settings, args: List[str], ignore_patterns: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Run CLI and ignore known harmless errors for idempotency.
    Returns result or None if ignored as already-in-desired-state.
    """
    try:
        return await run_cli(settings, args)
    except HTTPException as e:
        msg = str(e.detail)
        pats = ignore_patterns or []
        if any(p.lower() in msg.lower() for p in pats):
            logger.info("", extra={"event": "cli_run", "extras": {"args": _mask_secrets(args), "rc": "ignored", "reason": "idempotent"}})
            return None
        raise

def parse_accounts_table(text: str) -> List[Dict[str, Any]]:
    """Parse `everestctl accounts list` tabular output into a list of dicts.
    Expected header contains at least USER and ENABLED columns.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    # Split on 2+ spaces to accommodate column spacing
    header_cols = re.split(r"\s{2,}", lines[0])
    idx_user = header_cols.index("USER") if "USER" in header_cols else None
    idx_caps = header_cols.index("CAPABILITIES") if "CAPABILITIES" in header_cols else None
    idx_enabled = header_cols.index("ENABLED") if "ENABLED" in header_cols else None
    results: List[Dict[str, Any]] = []
    for line in lines[1:]:
        parts = re.split(r"\s{2,}", line)
        if idx_user is None or idx_enabled is None or len(parts) < 2:
            # Fallback: try simple split
            parts = line.split()
            if len(parts) < 2:
                continue
        item: Dict[str, Any] = {}
        try:
            if idx_user is not None and idx_user < len(parts):
                item["user"] = parts[idx_user]
            else:
                item["user"] = parts[0]
            if idx_caps is not None and idx_caps < len(parts):
                caps = parts[idx_caps]
                # Normalize [login,admin] -> ["login","admin"]
                caps_inner = caps.strip().lstrip("[").rstrip("]").strip()
                item["capabilities"] = [c.strip() for c in re.split(r"[,\s]+", caps_inner) if c.strip()] if caps_inner else []
            if idx_enabled is not None and idx_enabled < len(parts):
                item["enabled"] = parts[idx_enabled].lower() == "true"
            elif len(parts) >= 2:
                item["enabled"] = parts[-1].lower() == "true"
        except Exception:
            # Best-effort parsing; skip malformed lines
            continue
        results.append(item)
    return results

# -------------------- FastAPI App --------------------

app = FastAPI(title="Everestctl API", version="1.0.0")
app.add_middleware(LoggingMiddleware)
limiter = RateLimiter(get_settings().RATE_LIMIT_PER_MIN, get_settings().RATE_LIMIT_BURST)
app.add_middleware(RateLimitMiddleware, limiter=limiter)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().CORS_ALLOW_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup checks & DB init
@app.on_event("startup")
async def startup():
    s = get_settings()
    # Adjust logger level from settings
    try:
        logger.setLevel(s.LOG_LEVEL)
    except Exception:
        pass
    # Fail fast on missing API key
    if not s.API_KEY:
        logger.error("", extra={"event": "startup_fail", "extras": {"reason": "api_key missing"}})
        raise SystemExit("API_KEY is required")
    # Verify CLI presence unless explicitly skipped (tests/dev)
    if os.getenv("SKIP_CLI_CHECK", "").lower() in ("1", "true", "yes"):
        logger.info("", extra={"event": "startup_skip", "extras": {"reason": "SKIP_CLI_CHECK set"}})
    else:
        if not os.path.exists(s.EVERESTCTL_PATH) and not shutil.which(s.EVERESTCTL_PATH):
            logger.error(
                "",
                extra={"event": "startup_fail", "extras": {"reason": "everestctl missing", "path": s.EVERESTCTL_PATH}},
            )
            raise SystemExit("everestctl missing")
    init_db(s.DB_PATH)
    # Health probe (non-fatal if probe command fails, but log)
    try:
        await run_cli(s, [s.HEALTH_STARTUP_PROBE_CMD])
    except HTTPException as e:
        logger.error("", extra={"event": "startup_probe_fail", "extras": {"detail": str(e)}})

@app.on_event("shutdown")
async def shutdown():
    logger.info("", extra={"event":"shutdown","extras":{}})

# --------------- Health/Ready ---------------

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    return "ok"

@app.get("/readyz")
async def readyz():
    s = get_settings()
    try:
        res = await run_cli(s, ["version"])
        return {"status": "ready", "cli": res}
    except HTTPException as e:
        return JSONResponse({"status": "not_ready", "detail": str(e)}, status_code=503)

# --------------- Metrics ---------------

@app.get("/metrics")
async def metrics():
    if not get_settings().METRICS_ENABLED:
        raise HTTPException(404, "metrics disabled")
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

# --------------- Tenant APIs ---------------

def ensure_engine_allowed(settings: Settings, engine: str):
    allowed = [e.strip() for e in settings.ALLOWED_ENGINES.split(",") if e.strip()]
    if engine not in allowed:
        quota_violations.labels("engine_not_allowed").inc()
        raise HTTPException(400, f"engine '{engine}' is not allowed; allowed: {allowed}")

def ensure_cluster_quota(settings: Settings, tenant: str):
    used = get_counter(settings.DB_PATH, tenant)
    if used >= settings.MAX_CLUSTERS_PER_TENANT:
        quota_violations.labels("max_clusters").inc()
        raise HTTPException(403, f"cluster quota exceeded for tenant={tenant} (used={used}, max={settings.MAX_CLUSTERS_PER_TENANT})")

@app.post("/tenants/create")
async def create_tenant(req: TenantRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    ensure_engine_allowed(s, req.engine)
    ensure_cluster_quota(s, req.user)
    # Create namespace (idempotent)
    ns_args = ["namespaces", "add", req.namespace]
    if req.operators:
        ns_args += ["--operators", ",".join(req.operators)]
    await run_cli_idempotent(s, ns_args, ignore_patterns=["already exists", "exists", "AlreadyExists", "Conflict"])
    # Create local user if not using SSO
    if not s.SSO_ENABLED:
        await run_cli_idempotent(s, ["accounts", "create", "-u", req.user], ignore_patterns=["already exists", "exists", "AlreadyExists", "Conflict"])
        await run_cli(s, ["accounts", "set-password", "-u", req.user, "-p", req.password])
    # RBAC update with optional validation
    await rbac_add_validate_if_enabled(s, req.user, req.namespace)
    inc_counter(s.DB_PATH, req.user, +1)
    audit(
        s.DB_PATH,
        actor="api",
        action="tenant_create",
        target=req.user,
        details={"user": req.user, "namespace": req.namespace, "engine": req.engine},
    )
    logger.info("", extra={"event":"rbac_change","extras":{"op":"add","user":req.user,"namespace":req.namespace}})
    return {"status":"created", "user": req.user, "namespace": req.namespace}

@app.post("/tenants/delete")
async def delete_tenant(req: DeleteTenantRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    await run_cli_idempotent(s, ["namespaces", "delete", req.namespace], ignore_patterns=["not found", "does not exist", "NotFound"])
    await run_cli_idempotent(s, ["accounts", "delete", "-u", req.user], ignore_patterns=["not found", "does not exist", "NotFound"])
    await rbac_remove_validate_if_enabled(s, req.user, req.namespace)
    inc_counter(s.DB_PATH, req.user, -1)
    audit(s.DB_PATH, actor="api", action="tenant_delete", target=req.user, details=req.dict())
    logger.info("", extra={"event":"rbac_change","extras":{"op":"remove","user":req.user,"namespace":req.namespace}})
    return {"status":"deleted", "user": req.user, "namespace": req.namespace}

@app.post("/tenants/rotate-password")
async def rotate_password(req: RotatePasswordRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    if s.SSO_ENABLED:
        raise HTTPException(400, "password rotation is disabled when SSO is enabled")
    await run_cli(s, ["accounts", "set-password", "-u", req.user, "-p", req.new_password])
    audit(s.DB_PATH, actor="api", action="password_rotate", target=req.user, details={"user": req.user})
    return {"status":"rotated", "user": req.user}

@app.get("/tenants/{user}/quota")
async def get_quota(user: str, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    used = get_counter(s.DB_PATH, user)
    return {"tenant": user, "used_clusters": used, "max": s.MAX_CLUSTERS_PER_TENANT}

# --------------- Users APIs ---------------

@app.get("/users")
async def list_users(_: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    res = await run_cli(s, ["accounts", "list"])
    raw = ""
    if isinstance(res.get("stdout"), dict) and "raw" in res["stdout"]:
        raw = res["stdout"]["raw"]
    elif isinstance(res.get("stdout"), str):
        raw = res["stdout"]
    users = parse_accounts_table(raw)
    return {"users": users}

@app.post("/users/enable")
async def enable_user(req: UserOnlyRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    await run_cli(s, ["accounts", "enable", "-u", req.user])
    audit(s.DB_PATH, actor="api", action="user_enable", target=req.user, details={"user": req.user})
    return {"status": "enabled", "user": req.user}

@app.post("/users/disable")
async def disable_user(req: UserOnlyRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    await run_cli(s, ["accounts", "disable", "-u", req.user])
    audit(s.DB_PATH, actor="api", action="user_disable", target=req.user, details={"user": req.user})
    return {"status": "disabled", "user": req.user}

@app.post("/users/delete")
async def delete_user(req: UserOnlyRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    await run_cli(s, ["accounts", "delete", "-u", req.user])
    audit(s.DB_PATH, actor="api", action="user_delete", target=req.user, details={"user": req.user})
    return {"status": "deleted", "user": req.user}

@app.post("/users/set-password")
async def set_user_password(req: RotatePasswordRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    await run_cli(s, ["accounts", "set-password", "-u", req.user, "-p", req.new_password])
    audit(s.DB_PATH, actor="api", action="password_set", target=req.user, details={"user": req.user})
    return {"status":"password_set", "user": req.user}

@app.post("/users/create")
async def create_user(req: CreateUserRequest, _: None = Depends(api_key_auth), s: Settings = Depends(get_settings)):
    await run_cli(s, ["accounts", "create", "-u", req.user])
    if req.password:
        await run_cli(s, ["accounts", "set-password", "-u", req.user, "-p", req.password])
    if req.enabled is False:
        await run_cli(s, ["accounts", "disable", "-u", req.user])
    audit(s.DB_PATH, actor="api", action="user_create", target=req.user, details={"user": req.user, "enabled": bool(req.enabled)})
    return {"status": "created", "user": req.user, "enabled": bool(req.enabled)}

# --------------- Error Handling ---------------

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("", extra={"event":"unhandled_exception","extras":{"type": type(exc).__name__, "detail": str(exc)}})
    return JSONResponse({"error": "internal_error", "detail": str(exc)}, status_code=500)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Structured logging for HTTP exceptions too
    logger.info("", extra={"event":"http_exception","extras":{"status": exc.status_code, "detail": exc.detail}})
    return JSONResponse({"error": exc.detail if isinstance(exc.detail, str) else "http_error"}, status_code=exc.status_code)
