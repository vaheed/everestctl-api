# Everest Bootstrap API (FastAPI)

A small, async REST API that wraps everestctl and kubectl to create users/namespaces, apply quotas, and manage access in a Percona Everest cluster.

- Async jobs (submit -> poll -> result)
- Namespaces/operators provisioning + ResourceQuota/LimitRange
- Optional RBAC policy via ConfigMap
- Structured JSON logs and Prometheus metrics

---

## 1) Quick Start

Prereqs
- A kubeconfig with admin access to your Everest cluster
- Docker (or Python 3.11+ to run locally)

Start with Docker Compose
```
docker-compose up --build -d
export BASE_URL="http://localhost:8080"
export ADMIN_API_KEY="changeme"
```

Health checks
```
curl -sS "$BASE_URL/readyz"
curl -sS "$BASE_URL/healthz"
```

---

## 2) Authentication

- Send `X-Admin-Key: <your-key>` on every protected request.
- Default key is `changeme` (override via env `ADMIN_API_KEY`).

Example
```
export ADMIN_API_KEY="changeme"
# Include -H "X-Admin-Key: $ADMIN_API_KEY" on all calls below
```

---

## 3) Alice: End-to-End Lifecycle (Create -> Edit -> Delete)

We’ll walk through a full flow using cURL and jq.

Set common env
```
export BASE_URL="http://localhost:8080"
export ADMIN_API_KEY="changeme"
```

### 3.1 Create Alice (bootstrap)

Minimal request (namespace defaults to username)
```
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "username": "alice" }' | jq
```

Response contains a `job_id` and a `status_url`.

Poll job status
```
JOB_ID=$(curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{ "username": "alice" }' | jq -r .job_id)

echo "Job: $JOB_ID"
while true; do
  s=$(curl -sS "$BASE_URL/jobs/$JOB_ID" -H "X-Admin-Key: $ADMIN_API_KEY" | jq -r .status)
  echo "status=$s"; [[ "$s" == "succeeded" || "$s" == "failed" ]] && break
  sleep 1
done
```

Fetch detailed result (steps, summary)
```
curl -sS "$BASE_URL/jobs/$JOB_ID/result" -H "X-Admin-Key: $ADMIN_API_KEY" | jq
```

Create Alice with explicit namespace, operators, and quotas
```
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username": "alice",
        "namespace": "alice",
        "operators": {"postgresql": true, "mongodb": false, "mysql": false},
        "resources": {"cpu_cores": 2, "ram_mb": 2048, "disk_gb": 20, "max_databases": 3}
      }' | jq
```

### 3.2 Edit Alice

List accounts
```
curl -sS "$BASE_URL/accounts/list" -H "X-Admin-Key: $ADMIN_API_KEY" | jq
```

Change Alice password
```
curl -sS -X POST "$BASE_URL/accounts/password" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{ "username": "alice", "new_password": "S3cure!P@ssw0rd" }' | jq
```

Update quotas/limits for Alice namespace
```
curl -sS -X POST "$BASE_URL/namespaces/resources" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "namespace": "alice",
        "resources": {"cpu_cores": 4, "ram_mb": 4096, "disk_gb": 100, "max_databases": 5}
      }' | jq
```

Enable/disable operators for Alice namespace
```
# Enable PostgreSQL, disable MongoDB/MySQL
curl -sS -X POST "$BASE_URL/namespaces/operators" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "namespace": "alice",
        "operators": {"postgresql": true, "mongodb": false, "mysql": false}
      }' | jq
```

Suspend Alice (scale down apps, revoke RBAC entry)
```
curl -sS -X POST "$BASE_URL/accounts/suspend" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username": "alice",
        "namespace": "alice",
        "scale_statefulsets": true,
        "revoke_rbac": true
      }' | jq
```

### 3.3 Delete Alice (namespace + account + RBAC cleanup)
```
curl -sS -X POST "$BASE_URL/accounts/delete" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{ "username": "alice", "namespace": "alice" }' | jq
```

Notes
- The API first tries `everestctl namespaces remove <ns>`; if it fails, it falls back to `kubectl delete namespace <ns> --ignore-not-found=true --wait=false --timeout=30s` and separately deletes the account and revokes RBAC.
- The response includes a `steps` array with each action’s outcome.

---

## 4) Configuration Reference

Required
- `ADMIN_API_KEY` — header value for protected endpoints.

Common
- `KUBECONFIG` — path inside the container (default `/root/.kube/config`).
- `BOOTSTRAP_DEFAULT_PASSWORD` — default password if none provided on create.
- `BOOTSTRAP_DEFAULT_OPERATORS` — comma-separated defaults when request omits operators (e.g., `postgresql`).
- `EVEREST_RBAC_ENABLE_ON_BOOTSTRAP` — when true, enable and update the `everest-rbac` ConfigMap on bootstrap.
- `EVEREST_DB_COUNT_RESOURCES` — comma-separated CRDs for ResourceQuota `count/<crd>` limits (e.g., `perconapgclusters.pgv2.percona.com`).
- `ALLOWED_NAMESPACE_PREFIXES` — restrict allowed namespace prefixes (optional).
- `MAX_SUBPROC_CONCURRENCY` — cap concurrent CLI calls (default 16).
- `SAFE_SUBPROCESS_ENV` — if true, pass a minimal env to subprocesses.

---

## 5) Observability
- Logs: Structured JSON with `request_id`. Send `X-Request-ID` to propagate, echoed back on responses.
- Metrics: `GET /metrics` (Prometheus). Includes CLI latency histogram and call counters.

---

## 6) Running Locally (without Docker)
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.app:app --reload --host 0.0.0.0 --port 8080
```

---

## 7) Troubleshooting
- Delete "hangs" or is slow: the everestctl step may take up to its timeout. The fallback kubectl delete runs non-blocking (`--wait=false`). Check logs for the early "delete request" entry and subsequent step outputs.
- Suspend shows success with "no StatefulSets to scale": that’s an expected no-op when there are no StatefulSets in the namespace.
- Operator flags differ by CLI version: the API auto-falls back from `--operator.mysql` to `--operator.xtradb-cluster` if needed.

---

## 8) Reference
- Everest: https://docs.percona.com/everest/
- kubectl: https://kubernetes.io/docs/reference/kubectl/

