# Prompt: Production-Ready Async API to Bootstrap Users in Percona Everest (FastAPI + Jobs + Docker + CI)

Create a **production-ready**, minimal repository that implements an **async, job-based REST API** which bootstraps users and namespaces in a Percona **Everest** cluster using `everestctl` and `kubectl`.

**Reference docs:** https://docs.percona.com/everest/

---

## Goals

- Async HTTP API (FastAPI) with a **job queue** pattern:
  - Client submits work → receives **job id**
  - Client can **poll job status**
  - When finished, client can **retrieve job result**
- Bootstrapping workflow leverages **everestctl** and **kubectl**:
  - Create Everest account
  - Create/own namespace with operator flags
  - Apply ResourceQuota & LimitRange based on provided or default resources
  - Apply a Casbin-like RBAC policy mapping a role to the user
- Fully containerized:
  - Dockerfile installs **kubectl** and **everestctl**
  - docker-compose mounts host **KUBECONFIG**
- CI/CD via GitHub Actions:
  - **test → deploy** stages on push
- Endpoint auth via header: **`X-Admin-Key: <ADMIN_API_KEY>`**
- Include all list and deatils (like `everestctl account list`), plus **README**.

---

## Tech & Tooling

- **Python 3.11+**
- **FastAPI** (async) + **Uvicorn**
- **pytest**, **httpx** (tests)
- **Dockerfile** (installs `kubectl` & `everestctl`)
- **docker-compose** (mount host kubeconfig; pass env)
- **GitHub Actions**: two-stage **test** → **deploy** on `push`
- In-container: `KUBECONFIG` exported so `kubectl` & `everestctl` work

---

## API (Async Job Pattern)

All endpoints are **async**. Long-running work executes in a background task and is tracked by a **job id**.

### 1) Submit Bootstrap Job
`POST /bootstrap/users`

**Request JSON** (all fields optional except `username`):
```json
{
  "username": "alice",
  "namespace": "alice-ns",
  "operators": {
    "mongodb": true,
    "postgresql": true,
    "xtradb_cluster": false
  },
  "take_ownership": false,
  "resources": {
    "cpu_cores": 8,
    "ram_mb": 4096,
    "disk_gb": 100
  }
}
```

**Defaults** when omitted:
- `namespace`: `<username>`
- `operators`: all `false`
- `take_ownership`: `false`
- `resources`: `cpu_cores=2`, `ram_mb=2048`, `disk_gb=20`

**Response (202)**:
```json
{ "job_id": "<uuid>", "status_url": "/jobs/<uuid>" }
```

### 2) Check Job Status
`GET /jobs/{job_id}`

**Response**:
```json
{
  "job_id": "...",
  "status": "queued|running|succeeded|failed",
  "started_at": "...",
  "finished_at": null,
  "summary": "short human summary",
  "result_url": "/jobs/<uuid>/result"
}
```

### 3) Fetch Job Result
`GET /jobs/{job_id}/result` → returns full structured output **after completion**.

### 4) List Accounts (Pass-through)
`GET /accounts/list` with header `X-Admin-Key: <ADMIN_API_KEY>`.  
Runs:
```
everestctl account list
```
- If stdout is JSON → return `{"data": <json>}`
- If stdout is tabular/text → parse to structured JSON (pipe/whitespace split)
- On non-zero exit → **502** with `{ "error": "everestctl failed", "detail": "<stderr tail>" }`

**Auth**: All protected endpoints require `X-Admin-Key`; otherwise **401**.

---

## Bootstrap Workflow (Steps the Job Performs)

1) **Create Everest account**
   ```
   everestctl accounts create -u <username>
   ```

2) **Create (or take ownership of) namespace**
   ```
   everestctl namespaces add <namespace> \
     --operator.mongodb=<true|false> \
     --operator.postgresql=<true|false> \
     --operator.xtradb-cluster=<true|false> \
     [--take-ownership]
   ```

3) **Apply ResourceQuota & LimitRange** (via `kubectl apply -f -`):
   - Quota values come from `resources`:
     - CPU: `<cpu_cores>`
     - Memory: `<ram_mb>Mi`
     - Storage: `<disk_gb>Gi`
   - Include a **LimitRange** with sane defaults/requests (e.g., 25–50% of quota).

   **ResourceQuota (example)**
   ```yaml
   apiVersion: v1
   kind: ResourceQuota
   metadata:
     name: user-quota
   spec:
     hard:
       requests.cpu: "<cpu_cores>"
       requests.memory: "<ram_mb>Mi"
       requests.storage: "<disk_gb>Gi"
       limits.cpu: "<cpu_cores>"
       limits.memory: "<ram_mb>Mi"
   ```

4) **Apply RBAC Policy (Casbin-like)**:
   - Create a temp `policy.csv` with lines templated to the user’s namespace:
     ```
     p, role:alice, namespaces, read, <namespace>
     p, role:alice, database-engines, read, <namespace>/*
     p, role:alice, database-clusters, read, <namespace>/*
     p, role:alice, database-clusters, update, <namespace>/*
     p, role:alice, database-clusters, create, <namespace>/*
     p, role:alice, database-clusters, delete, <namespace>/*
     p, role:alice, database-cluster-credentials, read, <namespace>/*
     g, <username>, role:alice
     ```
   - Apply with a configurable command (env: `EVEREST_RBAC_APPLY_CMD`), e.g.:
     ```
     everestctl access-control import --file <policy.csv>
     ```
   - If not configured, **skip with warning** and set `rbac_applied=false` in the job result.

**Result Shape** returned by `/jobs/{id}/result`:
```json
{
  "inputs": { ...resolved defaults... },
  "steps": [
    { "name": "create_account", "command": "...", "exit_code": 0, "stdout": "...", "stderr": "" },
    { "name": "add_namespace", "command": "...", "exit_code": 0, "stdout": "...", "stderr": "" },
    { "name": "apply_resource_quota", "command": "kubectl apply -n <ns> -f -", "manifest_preview": "...", "exit_code": 0 },
    { "name": "apply_rbac_policy", "command": "<resolved cmd>", "exit_code": 0, "rbac_applied": true }
  ],
  "overall_status": "succeeded|failed",
  "summary": "User <u> and namespace <ns> created; quota applied; role bound."
}
```

---

## Security, Reliability, and Production Readiness

- **Auth**: `X-Admin-Key` per request; never log secrets.
- **Timeouts**: Set subprocess timeouts (e.g., 60s); return structured errors.
- **Graceful shutdown**: Handle SIGTERM/SIGINT (Uvicorn) to finish/abort jobs cleanly.
- **Health endpoints**: `/healthz` (liveness) & `/readyz` (readiness) returning minimal JSON.
- **Observability**: Structured JSON logging; include job id in logs; basic request logging with correlation id.
- **Validation**: Pydantic models; strict types; defaulting and coercion for `resources`.
- **Error handling**: Map known failure modes (`everestctl` missing, kubeconfig missing, non-zero exit) to 4xx/5xx with clear JSON.
- **In-memory job store (demo)**: Document that production should use Redis/RQ/Celery or a DB-backed queue and persistent job logs.
- **Least privilege**: Container runs as non-root where feasible (except where CLIs require root); read-only mounts for kubeconfig.
- **Rate limiting / auth hardening**: Optional, documented; ensure header auth is enforced for all mutating routes.

---

## Project Layout

```
.
├── app/
│   ├── app.py                  # FastAPI routes & startup/health
│   ├── jobs.py                 # in-memory job store & background tasks
│   ├── execs.py                # subprocess wrappers with timeouts
│   ├── k8s.py                  # YAML builders for ResourceQuota/LimitRange
│   ├── rbac.py                 # policy builder & apply logic
│   ├── parsers.py              # parse everestctl outputs (json/tabular)
│   └── __init__.py
├── tests/
│   ├── test_accounts_list.py
│   ├── test_bootstrap_job.py
│   └── test_parsers.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── .github/workflows/ci.yml
└── README.md
```

---

## Implementation Notes

### Exec wrapper
`run_cmd(cmd: list[str], input_text: str|None=None, timeout: int=60) -> {exit_code, stdout, stderr}` with:
- `text=True`, `check=False`, strip ANSI, truncate large outputs
- Record timestamps and include them in step results

### FastAPI specifics
- Use dependency to validate `X-Admin-Key`
- Use `BackgroundTasks` or an asyncio task registry (with `asyncio.Lock`) to track job states
- Return **202** immediately on submission with `job_id`

### Kube manifests (generated)
- ResourceQuota and LimitRange derived from inputs; sensible per-container defaults

### Parsers
- Try `json.loads`; on `JSONDecodeError`, try splitting lines into header/rows by `|` or whitespace

---

## Dockerfile (Production-Ready)

- Base: `python:3.11-slim`
- Install:
  - `curl`, `bash`, `ca-certificates`, `git`
  - **kubectl** (stable linux/amd64 via official release)
  - **everestctl**:
    - Preferred: download official binary to `/usr/local/bin/everestctl` and `chmod +x`
    - Fallback: `pip install everestctl` (leave comments on switching to binary)
- Copy code; `pip install -r requirements.txt`
- `ENV KUBECONFIG=/root/.kube/config`
- Create non-root user; `USER 10001` (adjust if CLIs require root, document trade-offs)
- `EXPOSE 8080`
- Entrypoint:
  ```
  uvicorn app.app:app --host 0.0.0.0 --port ${PORT:-8080}
  ```

---

## docker-compose.yml

- Service `api`:
  - Build from `.`
  - Env:
    - `ADMIN_API_KEY=${ADMIN_API_KEY:-changeme}`
    - `PORT=8080`
    - `KUBECONFIG=/root/.kube/config`
  - Ports: `8080:8080`
  - Volumes:
    - `${HOME}/.kube/config:/root/.kube/config:ro`  # mount host kubeconfig

---

## GitHub Actions: `.github/workflows/ci.yml`

- Trigger: `on: push`
- **test** job:
  - `ubuntu-latest`
  - Setup Python 3.11
  - Cache pip
  - `pip install -r requirements.txt`
  - `pytest -q`
- **deploy** job (needs: test):
  - Build: `docker build -t ${{ secrets.REGISTRY }}/${{ github.repository }}:${{ github.sha }} .`
  - Login: `docker login` using `${{ secrets.REGISTRY_USERNAME }}` / `${{ secrets.REGISTRY_PASSWORD }}`
  - Push image
  - Optional K8s deploy step (guard with `if:`):
    - If kube secrets exist (e.g., `KUBECONFIG_B64`), write kubeconfig and run a templated `kubectl apply -f`.
    - Otherwise echo “deploy skipped”.

---

## Requirements.txt

- `fastapi`
- `uvicorn[standard]`
- `pydantic`
- `pytest`
- `httpx`
- `python-dateutil`
- (optional) `uvloop` on Linux

---

## README Snippets (include verbatim)

**Quickstart**
```bash
docker-compose up --build -d
export BASE_URL="http://localhost:8080"
export ADMIN_API_KEY="changeme"
```

**Submit a bootstrap job**
```bash
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "username": "alice" }'
# => { "job_id": "...", "status_url": "/jobs/..." }
```

**Check job**
```bash
curl -sS "$BASE_URL/jobs/<JOB_ID>" -H "X-Admin-Key: $ADMIN_API_KEY"
```

**Get result**
```bash
curl -sS "$BASE_URL/jobs/<JOB_ID>/result" -H "X-Admin-Key: $ADMIN_API_KEY"
```

**List accounts**
```bash
curl -sS -X GET "$BASE_URL/accounts/list" -H "X-Admin-Key: $ADMIN_API_KEY"
```

**Notes**
- Works **async**: submit → poll → fetch result.
- Container expects kubeconfig mounted at `/root/.kube/config`.
---

## Percona Everest Docs

- Main docs: https://docs.percona.com/everest/
- Ensure CLI usage (`everestctl`) matches the version you install in the Dockerfile; keep the install URL/version configurable.

---

## Acceptance Criteria

- Submitting `POST /bootstrap/users` returns **202 + job id**.
- Polling `/jobs/{id}` shows progress.
- Fetching `/jobs/{id}/result` returns structured step-by-step output.
- Docker image runs with both CLIs available and honors `KUBECONFIG`.
- docker-compose mounts kubeconfig from host and exposes port 8080.
- CI runs tests then builds & pushes image (deploy optional).
- README documents setup, usage, and async job flow.

