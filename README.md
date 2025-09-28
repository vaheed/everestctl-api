# Everest Bootstrap API (FastAPI)

Async, job-based REST API to bootstrap users and namespaces in a Percona Everest cluster using everestctl and kubectl.

- Async endpoints with job IDs and polling
- Creates account, namespace with operator flags, applies ResourceQuota/LimitRange
- Optional RBAC policy application via ConfigMap (auto when enabled)
- Dockerfile installs kubectl and everestctl; docker-compose mounts host kubeconfig
- GitHub Actions CI: tests then image build/push

## Quickstart

```
docker-compose up --build -d
export BASE_URL="http://localhost:8080"
export ADMIN_API_KEY="changeme"
```

### Submit a bootstrap job
```
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "username": "alice" }'
# => { "job_id": "...", "status_url": "/jobs/..." }
```

### Check job
```
curl -sS "$BASE_URL/jobs/<JOB_ID>" -H "X-Admin-Key: $ADMIN_API_KEY"
```

### Get result
```
curl -sS "$BASE_URL/jobs/<JOB_ID>/result" -H "X-Admin-Key: $ADMIN_API_KEY"
```

### List accounts
```
curl -sS -X GET "$BASE_URL/accounts/list" -H "X-Admin-Key: $ADMIN_API_KEY"
```

Notes
- Works async: submit → poll → fetch result.
- Container expects kubeconfig mounted at `/root/.kube/config`.
- RBAC is applied automatically during bootstrap when `EVEREST_RBAC_ENABLE_ON_BOOTSTRAP=true`. The API updates the `everest-rbac` ConfigMap directly (enables it and merges a namespace-scoped policy for the user).
- Logs are structured JSON and include a request correlation id. Send `X-Request-ID` to propagate your own id; the API also returns `X-Request-ID` on all responses.
- If no operators are specified in the request, the API enables a default set to satisfy everestctl (at least one is required). Configure with `BOOTSTRAP_DEFAULT_OPERATORS` (default: `postgresql`). You can also specify explicitly: `{ "username": "alice", "operators": {"postgresql": true} }`.

## Configuration

- `ADMIN_API_KEY`: required header value for protected routes (default: changeme)
- `KUBECONFIG`: path to kubeconfig inside the container (default: /root/.kube/config)
- `BOOTSTRAP_DEFAULT_PASSWORD`: optional default password to use for newly created accounts when not provided in the request. If unset, the API generates a strong random password and returns it in the job result under `credentials`.
 - `BOOTSTRAP_DEFAULT_OPERATORS`: comma-separated list of operators to enable when the request omits them (choices: `mongodb`, `postgresql`, `mysql`/`xtradb_cluster`). Default: `postgresql`.
 - `EVEREST_RBAC_ENABLE_ON_BOOTSTRAP`: when truthy (`true/1/yes/on`), enable the Everest RBAC ConfigMap (`data.enabled="true"`) and merge a namespace-scoped policy for the user during bootstrap. Default: `true` in docker-compose.

## Endpoints

- POST `/bootstrap/users` → 202, returns `{job_id, status_url}`
- GET `/jobs/{id}` → job status
- GET `/jobs/{id}/result` → job result with step-by-step outputs
- GET `/accounts/list` → parsed output from `everestctl accounts list`
- POST `/accounts/password` → change a user's password
- POST `/namespaces/resources` → apply/update ResourceQuota + LimitRange
- POST `/namespaces/operators` → enable operators for a namespace
- POST `/accounts/suspend` → suspend a user (scale down namespace + revoke RBAC)
- POST `/accounts/delete` → delete user and clean up namespace
- GET `/healthz`, GET `/readyz`

## Limiting Resources via the API

Set limits when submitting the bootstrap job. The API applies a ResourceQuota and LimitRange in the user namespace.

- CPU/Memory/Storage caps: set in `resources`
- Max database clusters per namespace: set `resources.max_databases` and configure counted CRDs via an env var

Example request
```
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "username": "alice",
        "namespace": "alice-ns",
        "operators": {"mongodb": true, "postgresql": true, "mysql": false},
        "resources": {"cpu_cores": 4, "ram_mb": 4096, "disk_gb": 100, "max_databases": 3}
      }'
```

To enforce max database count, set this env on the API container to the CRDs you want to count (comma‑separated):

```
# Example values – verify with: kubectl get crd | grep -i percona
export EVEREST_DB_COUNT_RESOURCES="perconaservermongodbs.psmdb.percona.com,perconapgclusters.pgv2.percona.com,perconaxtradbclusters.pxc.percona.com"
```

Notes
- The API applies ResourceQuota keys like `count/<resource>` for each listed CRD when `resources.max_databases` is provided.
- CRD names vary by operator/version. Use `kubectl get crd` to confirm the plural.group string for your cluster.
- CPU/Memory limits are applied to both requests and limits; adjust `app/k8s.py` if you need different ratios.

## Development

- Python 3.11+
- `pip install -r requirements.txt`
- `pytest -q`
- Run locally (without Docker): `uvicorn app.app:app --reload --port 8080`

## Docker

- `docker build -t everestctl-api .`
- `docker run -p 8080:8080 -e ADMIN_API_KEY=changeme -v $HOME/.kube/config:/root/.kube/config:ro everestctl-api`

Build-time args
- Pin CLI versions at build time for reproducibility:
  - `--build-arg KUBECTL_VERSION=v1.30.5`
  - `--build-arg EVERESTCTL_VERSION=vX.Y.Z` (or leave as `latest`)
- Example: `docker build --build-arg KUBECTL_VERSION=v1.30.5 --build-arg EVERESTCTL_VERSION=v1.0.0 -t everestctl-api .`

Runtime port
- The image respects `PORT` env (default 8080): `-e PORT=8080 -p 8080:8080`

## Security and production notes

- Header auth only, keep ADMIN_API_KEY secret (use vault/secret store)
- Subprocesses use timeouts; errors are captured and surfaced
- In-memory job store: use Redis/Celery/RQ for persistence in production
- Consider rate limiting and structured logging with correlation IDs
  - This API emits JSON logs with `request_id` for correlation
- Dependencies are version-ranged for stability; pin exactly if desired

## Reference

- Percona Everest docs: https://docs.percona.com/everest/

## API Examples (Alice and Bob)

Set shared env first:

```
export BASE_URL="http://localhost:8080"
export ADMIN_API_KEY="changeme"
```

1) Bootstrap Alice and Bob

```
# Alice minimal (namespace defaults to username)
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice"}'
# { "job_id": "6ba...", "status_url": "/jobs/6ba..." }

# Bob with explicit namespace, operators and resources
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username":"bob",
        "namespace":"team-bob",
        "operators": {"mongodb": true, "postgresql": true, "mysql": false},
        "resources": {"cpu_cores": 2, "ram_mb": 2048, "disk_gb": 50, "max_databases": 5}
      }'
# { "job_id": "9d3...", "status_url": "/jobs/9d3..." }
```

Poll and fetch result:

```
curl -sS "$BASE_URL/jobs/<JOB_ID>" -H "X-Admin-Key: $ADMIN_API_KEY"
# { "job_id": "...", "status": "running|succeeded|failed", "result_url": "/jobs/.../result", ... }

curl -sS "$BASE_URL/jobs/<JOB_ID>/result" -H "X-Admin-Key: $ADMIN_API_KEY"
# {
#   "inputs": {"username":"bob", "namespace":"team-bob", ...},
#   "steps": [
#     {"name": "create_account", "exit_code": 0, "stdout": "..."},
#     {"name": "add_namespace", "exit_code": 0, "stdout": "..."},
#     {"name": "apply_resource_quota", "exit_code": 0},
#     {"name": "apply_rbac_policy", "rbac_applied": false }
#   ],
#   "overall_status": "succeeded",
#   "summary": "User bob and namespace team-bob created; quota applied; RBAC skipped."
# }
```

2) Accounts list

```
curl -sS "$BASE_URL/accounts/list" -H "X-Admin-Key: $ADMIN_API_KEY"
# { "data": { "items": [ { "name": "alice" }, { "name": "bob" } ] } }
```

3) Change Bob's password

```
curl -sS -X POST "$BASE_URL/accounts/password" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"bob", "new_password":"S3cure!P@ss"}'
# { "ok": true, "username": "bob" }
```

4) Update Alice namespace resources (including max databases)

```
# Ensure EVEREST_DB_COUNT_RESOURCES is configured in the API env
curl -sS -X POST "$BASE_URL/namespaces/resources" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "namespace": "alice",
        "resources": {"cpu_cores": 4, "ram_mb": 4096, "disk_gb": 100, "max_databases": 3}
      }'
# { "ok": true, "namespace": "alice", "applied": true }
```

5) Enable operators for Bob's namespace

```
curl -sS -X POST "$BASE_URL/namespaces/operators" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "namespace": "team-bob",
        "operators": {"mongodb": true, "postgresql": true, "mysql": false}
      }'
# { "ok": true, "namespace": "team-bob" }
```

6) Suspend Bob (scale down and revoke access)

```
curl -sS -X POST "$BASE_URL/accounts/suspend" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username": "bob",
        "namespace": "team-bob",
        "scale_statefulsets": true,
        "revoke_rbac": true
      }'
# { "ok": true, "username": "bob", "namespace": "team-bob", "steps": [ ... ] }
```

7) Delete Alice completely (namespace + account + RBAC cleanup)

```
curl -sS -X POST "$BASE_URL/accounts/delete" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username": "alice",
        "namespace": "alice"
      }'
# { "ok": true, "username": "alice", "namespace": "alice", "steps": [ ... ] }
```

## Auth & Security

- Single key header auth via `X-Admin-Key` (env: `ADMIN_API_KEY`).
- Optional multi-key rotation: set `ADMIN_API_KEYS_JSON` to a JSON map like `{ "k1": "secret1", "k2": "secret2" }` and send both headers `X-Admin-Key-Id: k1` and `X-Admin-Key: secret1`.
- Correlated JSON logs: send `X-Request-ID` to propagate your trace id; the API echoes it on responses.
- Prometheus metrics at `/metrics` (enable by installing `prometheus-client`, already in requirements).

## Operational Controls

- `ENABLE_K8S_NAMESPACE_DELETE_FALLBACK`: when `true`, allow fallback `kubectl delete namespace` if `everestctl namespaces remove` fails. Default: disabled.
- `ALLOWED_NAMESPACE_PREFIXES`: optional comma-separated list of allowed namespace prefixes (e.g. `user-,team-`). If set, incoming namespaces must start with one of these prefixes.
- `MAX_SUBPROC_CONCURRENCY`: cap concurrent CLI calls (default: 16). `SAFE_SUBPROCESS_ENV=1` restricts environment passed to subprocesses to a minimal allowlist.

## Metrics

- Exposed at `GET /metrics` in Prometheus exposition format.
- Includes CLI latency histogram `everest_api_cli_latency_seconds` and call counter `everest_api_cli_calls_total` (labels: tool, exit_code).

Quick examples for alice15

```
# Suspend alice15 (scale down namespace and revoke RBAC entries)
curl -sS -X POST "$BASE_URL/accounts/suspend" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username": "alice15",
        "namespace": "alice15",
        "scale_statefulsets": true,
        "revoke_rbac": true
      }'

# Delete alice15 completely (namespace + account + RBAC cleanup)
curl -sS -X POST "$BASE_URL/accounts/delete" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "username": "alice15",
        "namespace": "alice15"
      }'
```

What suspend/delete do
- Suspend: tries to deactivate the account (if supported by your everestctl), scales down all StatefulSets in the namespace to 0, and removes the user’s role/bindings from the `everest-rbac` ConfigMap.
- Delete: removes the namespace (prefers `everestctl namespaces remove`, falls back to `kubectl delete namespace`), revokes the user in RBAC, and deletes the account (tries `everestctl accounts delete|remove`).

Notes
- RBAC revocation edits the `everest-rbac` ConfigMap in `everest-system` and removes lines for the specific user role/binding. This assumes you are using ConfigMap-based RBAC.
- Suspend success is reported if at least one action (scale down or RBAC revoke) succeeds; account deactivation is best-effort since flags vary by version.
Password during bootstrap
- The API calls `everestctl accounts create` non‑interactively by supplying a password to avoid TTY prompts.
- You can pass a password in the request body: `{ "username": "alice", "password": "S3cure!" }`.
- If omitted, it uses `BOOTSTRAP_DEFAULT_PASSWORD` if set; otherwise it generates a strong password and returns it in the job result (`credentials`).
