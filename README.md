# Everest Bootstrap API (FastAPI)

Async, job-based REST API to bootstrap users and namespaces in a Percona Everest cluster using everestctl and kubectl.

- Async endpoints with job IDs and polling
- Creates account, namespace with operator flags, applies ResourceQuota/LimitRange
- Optional RBAC policy application via EVEREST_RBAC_APPLY_CMD
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
- To enable RBAC apply, set `EVEREST_RBAC_APPLY_CMD="everestctl access-control import --file {file}"` or point to your tooling. If not set, RBAC step is skipped.
- Logs are structured JSON and include a request correlation id. Send `X-Request-ID` to propagate your own id; the API also returns `X-Request-ID` on all responses.

## Configuration

- `ADMIN_API_KEY`: required header value for protected routes (default: changeme)
- `KUBECONFIG`: path to kubeconfig inside the container (default: /root/.kube/config)
- `EVEREST_RBAC_APPLY_CMD`: optional command template to apply RBAC, with `{file}` placeholder for a temp policy file

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

What suspend/delete do
- Suspend: tries to deactivate the account (if supported by your everestctl), scales down all StatefulSets in the namespace to 0, and removes the user’s role/bindings from the `everest-rbac` ConfigMap.
- Delete: removes the namespace (prefers `everestctl namespaces remove`, falls back to `kubectl delete namespace`), revokes the user in RBAC, and deletes the account (tries `everestctl accounts delete|remove`).

Notes
- RBAC revocation edits the `everest-rbac` ConfigMap in `everest-system` and removes lines for the specific user role/binding. This assumes you are using ConfigMap-based RBAC.
- Suspend success is reported if at least one action (scale down or RBAC revoke) succeeds; account deactivation is best-effort since flags vary by version.
