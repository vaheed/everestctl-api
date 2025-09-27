# 🔧 - Production‑grade **Everest Tenant Bootstrap Proxy** (FastAPI + `everestctl`)

> **Goal:** Design & generate a hardened HTTP API that wraps **`everestctl`** for tenant lifecycle (users, namespaces, RBAC) and per‑namespace resource policies (CPU, memory, max DB users) using **Kubernetes CRDs** — with strict security, stability, observability, tests, and deploy artifacts.

**Authoritative docs to follow for CLI parity:**  
- Users: https://docs.percona.com/everest/administer/manage_users.html  
- Namespaces: https://docs.percona.com/everest/administer/manage_namespaces.html  
- RBAC (policy.csv / everest-rbac ConfigMap / validate & can): https://docs.percona.com/everest/administer/rbac.html

---

## 0) Ground rules (must follow exactly)

- **Only** call `everestctl` with **validated, allow‑listed argv** (never use a shell).
- Match **current Percona Everest CLI** for **user** and **namespace** commands and **RBAC** behavior per the docs above.
- RBAC is stored in ConfigMap **`everest-rbac`** (namespace `everest-system`), with `data.enabled: "true"` and `data.policy.csv` content.
- Implement **per‑tenant CRD** (Kubernetes CustomResourceDefinition) for resource policy (CPU/memory/user limits) and **bind** it to the tenant’s namespace; the proxy enforces these limits before cluster creation and maintains usage counters.

> ✅ Commands that MUST exist & be wired as API endpoints (verify at startup with `everestctl help` and fail fast if missing):
>
> - `everestctl accounts create -u <username>`
> - `everestctl accounts set-password -u <username>`
> - `everestctl accounts list`
> - `everestctl accounts delete -u <username>`
> - `everestctl namespaces add <NAMESPACE> [--operator.mongodb=<bool>] [--operator.postgresql=<bool>] [--operator.xtradb-cluster=<bool>] [--take-ownership]`
> - `everestctl namespaces update <NAMESPACE>`  *(adding operators; removing not supported)*
> - `everestctl namespaces remove <NAMESPACE> [--keep-namespace]`
> - `everestctl settings rbac validate [--policy-file <path>]`
> - `everestctl settings rbac can [--policy-file <path>]`

---

## 1) Deliverables (repository layout)

Create a complete repo with **working code**, **tests**, and **deploy** artifacts:

```
.
├── app.py                   # FastAPI app (routers, DI, OpenAPI tags)
├── cli.py                   # Safe everestctl runner (argv allowlist, timeouts, retries)
├── rbac.py                  # RBAC file ops (atomic policy.csv, validate/apply)
├── quotas.py                # Quota CRUD, enforcement, usage counters
├── crd.py                   # K8s CRD manifests + controller shim (via kubernetes client or kubectl)
├── db.py                    # SQLite (default) or Postgres, migrations, audit writer
├── auth.py                  # API key auth (constant-time), rate limiting, idempotency keys
├── logging_setup.py         # JSON logs + request_id middleware
├── schemas.py               # Pydantic v2 models, strict validation
├── kubernetes/
│   ├── crd-tenantresourcepolicy.yaml   # CRD definition (v1)
│   └── rbac-reader-role.yaml           # Optional read-only role for operators
├── gunicorn_conf.py         # JSON access logs, sane worker defaults
├── entrypoint.sh            # Preflight checks; tini; fail fast on misconfig
├── Dockerfile               # slim, non-root, tini, everestctl preinstalled
├── docker-compose.yml       # Volumes for policy.csv & DB; healthchecks
├── requirements.txt
├── .env.example             # All envs with docs
├── README.md                # Step-by-step guide + cURL for every endpoint
└── tests/
    ├── conftest.py
    ├── test_bootstrap.py
    ├── test_rbac.py
    ├── test_quotas.py
    ├── test_cli.py
    └── test_crd.py
```

---

## 2) Core capabilities (spec)

### 2.1 Secure admin API (all require `X-Admin-Key`)

- **Health/Readiness**
  - `GET /healthz` → 200 if process alive.
  - `GET /readyz` → runs cached `everestctl version`, checks DB & `policy.csv` path RW.

- **Tenant bootstrap** (idempotent):
  - `POST /bootstrap/tenant`
    ```json
    {
      "username":"alice",
      "password":"StrongP@ssw0rd",
      "namespace":"ns-alice",
      "operators":{"postgresql":true,"mongodb":false,"xtradb_cluster":true},
      "idempotency_key": "optional-guid"
    }
    ```
  - Steps:
    1) `everestctl namespaces add <ns> [--operator.*=true|false]`
    2) `everestctl accounts create -u <username>` *(skip if exists)*
    3) `everestctl accounts set-password -u <username>`
    4) Append **RBAC** for `<ns>` and `<user>`; **atomic** `policy.csv`; `settings rbac validate` then apply (if `APPLY_RBAC=true`).
    5) Create or update **TenantResourcePolicy** CR in the namespace (defaults).
    6) Initialize **limits** and **usage** rows in DB; audit all steps.

- **Safe CLI wrappers** (strict argv)
  - `POST   /cli/accounts/create` → `{ "username": "alice" }`
  - `POST   /cli/accounts/set-password` → `{ "username":"alice","new_password":"..." }`
  - `GET    /cli/accounts/list`
  - `DELETE /cli/accounts` → `{ "username":"alice" }`
  - `POST   /cli/namespaces/add` → `{ "namespace":"ns-alice","operators":{"postgresql":false},"take_ownership":false }`
  - `POST   /cli/namespaces/update` → `{ "namespace":"ns-alice","operators":{"mongodb":true} }`
  - `DELETE /cli/namespaces/remove` → `{ "namespace":"ns-alice","keep_namespace":false }`

- **RBAC admin**
  - `POST /rbac/append` → append validated `p`/`g` lines; backup + atomic write.
  - `POST /rbac/can` → `{"user":"alice","resource":"database-clusters","verb":"create","object":"ns-alice/*"}` → uses `everestctl settings rbac can`.
  - Policy record format: `p, <subject>, <resource-type>, <action>, <resource-name>` and `g, <user>, <role>`.

- **Per‑namespace quotas/limits**
  - `PUT /limits` → upsert `{ "namespace":"ns-alice","max_clusters":3,"allowed_engines":["postgresql","mysql"],"cpu_limit_cores":4.0,"memory_limit_bytes":17179869184,"max_db_users":20 }`
  - `GET /limits/{namespace}`
  - `POST /enforce/cluster-create` → validate engine & headroom for CPU/memory; return **403/429** on violation with clear message.
  - `POST /usage/register-cluster` → `{ "namespace":"ns-alice","op":"create"|"delete","cpu_cores":1,"memory_bytes":2147483648 }` (transactional counters; never negative).
  - `POST /usage/register-db-user` → `{ "namespace":"ns-alice","op":"create"|"delete" }`

- **Day‑2 ops**
  - `DELETE /users` → `{ "username":"alice","namespace":"ns-alice","remove_rbac":true }`
  - `DELETE /namespaces` → `{ "namespace":"ns-alice","force":false }`
  - `POST   /users/rotate-password` → `{ "username":"alice","new_password":"..." }`

- **Admin views**
  - `GET /tenants`, `GET /audit` (filters: actor, namespace, action, since)
  - `GET /users/raw`, `GET /namespaces/raw` (lightweight CLI list with caching)

### 2.2 Kubernetes CRD for tenant resource policy

Create a namespace‑scoped CRD (group `everest.local`, version `v1`, kind `TenantResourcePolicy`, plural `tenantresourcepolicies`).

**CRD spec (YAML):**
```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: tenantresourcepolicies.everest.local
spec:
  group: everest.local
  scope: Namespaced
  names:
    kind: TenantResourcePolicy
    plural: tenantresourcepolicies
    singular: tenantresourcepolicy
    shortNames: [trp]
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [limits, selectors]
              properties:
                limits:
                  type: object
                  properties:
                    cpuCores: { type: number, minimum: 0 }
                    memoryBytes: { type: integer, minimum: 0 }
                    maxClusters: { type: integer, minimum: 0 }
                    maxDbUsers: { type: integer, minimum: 0 }
                selectors:
                  type: object
                  properties:
                    engines:
                      type: array
                      items:
                        type: string
                        enum: [postgresql, mysql, mongodb, xtradb_cluster]
```

**Example per‑tenant object (apply to `ns-alice`):**
```yaml
apiVersion: everest.local/v1
kind: TenantResourcePolicy
metadata:
  name: resource-policy
  namespace: ns-alice
spec:
  limits:
    cpuCores: 4
    memoryBytes: 17179869184  # 16Gi
    maxClusters: 3
    maxDbUsers: 20
  selectors:
    engines: ["postgresql","mysql"]
```

The proxy:
- Creates/updates this CR when bootstrapping or when `/limits` changes.
- Reads (or watches) it to drive enforcement.

---

## 3) Security hardening

- **Auth**: require `X-Admin-Key` (constant‑time compare). Optionally allow `Authorization: Bearer <token>` if configured.
- **Rate limiting**: token‑bucket per **client IP** and **route** (`RATE_QPS`, `RATE_BURST`).
- **Input validation**:
  - `username`, `namespace`: regex `^[a-z0-9]([-a-z0-9]{1,61}[a-z0-9])?$` (K8s‑safe).
  - Strict enums for engines/actions; request body size limit.
- **Subprocess safety**: build argv from allow‑listed verbs only:
  - `["everestctl","accounts","create","-u",username]`
  - `["everestctl","accounts","set-password","-u",username]`
  - `["everestctl","accounts","list"]`
  - `["everestctl","accounts","delete","-u",username]`
  - `["everestctl","namespaces","add",ns,"--operator.postgresql=true|false","--operator.mongodb=true|false","--operator.xtradb-cluster=true|false","--take-ownership"(optional)]`
  - `["everestctl","namespaces","update",ns]`
  - `["everestctl","namespaces","remove",ns,"--keep-namespace"(optional)]`
  - `["everestctl","settings","rbac","validate","--policy-file",path?]`
  - `["everestctl","settings","rbac","can","--policy-file",path?]`
- **CLI resilience**: env‑driven timeouts (`CLI_TIMEOUT_SEC`) and retries (`CLI_RETRIES`) with exponential backoff on transient errors; redact secrets in logs.
- **RBAC file safety**: file‑lock, write `policy.csv.tmp`, fsync, atomic rename; rotate backups `policy.csv.bak.<RFC3339>`.
- **Logging**: JSON logs with `event` names (`http_request`, `cli_run`, `rbac_change`, `quota_enforce`, etc.).
- **Idempotency**: honor `Idempotency-Key` for POST; store short‑lived keys in DB.

---

## 4) Observability & audit

- **Metrics** (optional via `METRICS_ENABLED=true`):
  - `http_requests_total`, `http_latency_seconds`
  - `cli_invocations_total{cmd,exit_code}`
  - `quota_violations_total`
  - `rate_limit_block_total`
- **Audit log** (append‑only): who/when/what, namespace/user, request/response hashes, `cli_exit_code/stdout/stderr` (size‑capped).
- **Readiness** checks include `everestctl version` and (if CRDs enabled) `kubectl version --client`.

---

## 5) Configuration (env)

| Var | Purpose | Default |
|---|---|---|
| `ADMIN_API_KEY` | **Required** | *(none)* |
| `POLICY_FILE` | RBAC policy path | `/var/lib/everest/policy/policy.csv` |
| `APPLY_RBAC` | Validate/apply on change | `true` |
| `SQLITE_DB` | SQLite path | `/var/lib/everest/data/tenant_proxy.db` |
| `DATABASE_URL` | Postgres (optional) | *(unset)* |
| `EVERESTCTL_BIN` | CLI path | `everestctl` |
| `EVEREST_URL` | Everest API URL (outside cluster) | *(unset)* |
| `EVEREST_TOKEN` | Everest token (outside cluster) | *(unset)* |
| `CLI_TIMEOUT_SEC` | Per call timeout | `30` |
| `CLI_RETRIES` | Retries | `2` |
| `RATE_QPS`/`RATE_BURST` | Rate limiting | `10` / `20` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `METRICS_ENABLED` | `/metrics` | `false` |

---

## 6) RBAC content (per‑tenant role & binding)

**Minimal, namespace‑scoped policy lines (append to `policy.csv` atomically):**
```
# Role for ns-alice
p, role:tenant-ns-alice, namespaces, read, ns-alice
p, role:tenant-ns-alice, database-engines, read, ns-alice/*
p, role:tenant-ns-alice, database-clusters, *, ns-alice/*
p, role:tenant-ns-alice, database-cluster-backups, *, ns-alice/*
p, role:tenant-ns-alice, database-cluster-restores, *, ns-alice/*
p, role:tenant-ns-alice, database-cluster-credentials, read, ns-alice/*
p, role:tenant-ns-alice, backup-storages, *, ns-alice/*
p, role:tenant-ns-alice, monitoring-instances, *, ns-alice/*

g, alice, role:tenant-ns-alice
```

**Validate & test:**
- `everestctl settings rbac validate [--policy-file <path>]`
- `everestctl settings rbac can --policy-file <path>` (e.g., can `alice` `create` `database-clusters` on `ns-alice/*`)

---

## 7) Docker & deployment

- **Dockerfile**: `python:3.11-slim`, install `tini` and `everestctl`, create user `appuser`, copy app, `pip install --no-cache-dir -r requirements.txt`.
- **entrypoint.sh**: fail fast if `ADMIN_API_KEY` or `everestctl` missing; touch/mkdir `policy.csv` dir; run `gunicorn -c gunicorn_conf.py app:app`.
- **docker-compose.yml**: volumes `./var/policy:/var/lib/everest/policy`, `./var/data:/var/lib/everest/data`; env from `.env`; healthchecks on `/healthz` and `/readyz`.

---

## 8) Tests (pytest)

- Mock `subprocess.run` to simulate success / timeouts / non‑zero exits; password prompts for `set-password`/`create`.
- Assert:
  - **Correct argv** for every CLI wrapper (no shell, no unexpected flags).
  - Idempotent bootstrap.
  - Atomic `policy.csv` with backup & rollback on failure.
  - Quota enforcement (max clusters, engines, CPU/memory, db users).
  - Counters stay consistent under concurrency.
  - Auth, rate‑limit, idempotency behaviors.
  - CRD create/update and readback round‑trip.

---

## 9) Example cURL (mirror to README)

```bash
# Health/ready
curl -fsS localhost:8080/healthz
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8080/readyz

# Bootstrap
curl -X POST localhost:8080/bootstrap/tenant \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"AnotherP@ss1","namespace":"ns-bob",
       "operators":{"postgresql":true,"mongodb":true,"xtradb_cluster":false}}'

# Alice bootstrap
curl -X POST localhost:8080/bootstrap/tenant \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"StrongP@ssw0rd","namespace":"ns-alice",
      "operators":{"postgresql":true,"mongodb":false,"xtradb_cluster":true}
    },
    {
      "username":"bob","password":"AnotherP@ss1","namespace":"ns-bob",
       "operators":{"postgresql":true,"mongodb":false,"xtradb_cluster":true}}'

# CLI accounts
curl -X POST   localhost:8080/cli/accounts/create -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"username":"alice"}'

curl -X POST   localhost:8080/cli/accounts/set-password -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"username":"alice","new_password":"NewP@ss"}'

curl -X GET    localhost:8080/cli/accounts/list -H "X-Admin-Key: $ADMIN_API_KEY"

curl -X DELETE localhost:8080/cli/accounts -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"username":"alice"}'

# Namespaces
curl -X POST localhost:8080/cli/namespaces/add -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","operators":{"postgresql":false}}'

curl -X POST localhost:8080/cli/namespaces/update -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"namespace":"ns-alice","operators":{"mongodb":true}}'

curl -X DELETE localhost:8080/cli/namespaces/remove -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"namespace":"ns-alice","keep_namespace":true}'

# RBAC check
curl -X POST localhost:8080/rbac/can -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","resource":"database-clusters","verb":"create","object":"ns-alice/*"}'

# Limits / enforce / usage
curl -X PUT localhost:8080/limits -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","max_clusters":3,"allowed_engines":["postgresql","mysql"],
       "cpu_limit_cores":4,"memory_limit_bytes":17179869184,"max_db_users":20}'

curl -X POST localhost:8080/enforce/cluster-create -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","engine":"postgresql","cpu_request_cores":1,"memory_request_bytes":2147483648}'

curl -X POST localhost:8080/usage/register-cluster -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"create","cpu_cores":1,"memory_bytes":2147483648}'

# CRD (via kubectl)
kubectl apply -f kubernetes/crd-tenantresourcepolicy.yaml
kubectl -n ns-alice apply -f - <<'YAML'
apiVersion: everest.local/v1
kind: TenantResourcePolicy
metadata: { name: resource-policy }
spec:
  limits: { cpuCores: 4, memoryBytes: 17179869184, maxClusters: 3, maxDbUsers: 20 }
  selectors: { engines: ["postgresql","mysql"] }
YAML
```

---

## 10) Acceptance checklist

- `/readyz` returns 200 and confirms **`everestctl`** callable.
- Bootstrap performs: namespace add → user create → set password → RBAC append/validate → CRD upsert → DB init.
- CLI wrappers reject unsafe input; argv equals spec.
- Quotas enforced; counters consistent under concurrency.
- `policy.csv` writes are atomic with backups & rollback.
- JSON logs & (optional) Prometheus metrics.
- Tests pass in container; README examples work.
