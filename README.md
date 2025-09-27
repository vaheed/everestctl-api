Everest Tenant Bootstrap Proxy

Production-grade FastAPI API that wraps everestctl for tenant lifecycle and per-namespace quotas with atomic RBAC policy management.

Quick start
- Copy `.env.example` to `.env` and set `ADMIN_API_KEY`.
- Set `GHCR_OWNER` in `.env` to your GitHub user/org.
- Ensure your kubeconfig exists (default `${HOME}/.kube/config`).
- Pull and run: `docker compose up` (compose pulls `ghcr.io/$GHCR_OWNER/everestctl-api:latest`).
- Health: `curl -fsS localhost:8080/healthz`
- Ready: `curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8080/readyz`

Endpoints
- POST /bootstrap/tenant
- CLI wrappers under /cli/*
- RBAC: POST /rbac/append, POST /rbac/can
- Limits: PUT /limits, GET /limits/{namespace}
- Enforcement: POST /enforce/cluster-create
- Usage: POST /usage/register-cluster, POST /usage/register-db-user
- Day-2: DELETE /users, DELETE /namespaces, POST /users/rotate-password
- Admin: GET /tenants, GET /audit, GET /users/raw, GET /namespaces/raw

Notes
- The service allow-lists everestctl argv and never uses a shell.
- RBAC writes to `POLICY_FILE` atomically with timestamped backups and validates via everestctl.
- Idempotency: POST honors Idempotency-Key and replays responses.
- Metrics: enable with `METRICS_ENABLED=true` then GET /metrics.
- CRD manifests under `kubernetes/`. The app best-effort applies per-tenant policy via kubectl if present.
- kubectl and everestctl are installed in the image; KUBECONFIG is exported to `/data/kubeconfig` and docker-compose mounts `${KUBECONFIG_HOST_PATH:-${HOME}/.kube/config}` there (read-only).

Testing
- `pytest` (CLI calls are mocked by tests).

CI/CD
- GitHub Actions workflow `.github/workflows/ci.yml` runs tests on push/PR.
- On pushes (non‑PR), builds and pushes Docker image to GHCR with tags:
  - `sha-<shortsha>` for all branches
  - `latest` for `main`
  - `<git tag>` for tag refs

API Endpoints and Examples
- All admin endpoints require `X-Admin-Key: $ADMIN_API_KEY`.
- Replace values like ns-alice with your namespaces if needed.

Environment setup
```bash
export ADMIN_API_KEY=change-me
# Base URL of the running service
BASE_URL=http://localhost:8080
```

Health and readiness
```bash
curl -fsS $BASE_URL/healthz
curl -fsS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/readyz
```

Bootstrap tenants (Alice and Bob)
```bash
# Alice
curl -sS -X POST $BASE_URL/bootstrap/tenant \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Idempotency-Key: 11111111-1111-1111-1111-111111111111" \
  -H "Content-Type: application/json" \
  -d '{
        "username":"alice",
        "password":"StrongP@ssw0rd",
        "namespace":"ns-alice",
        "operators":{"postgresql":true,"mongodb":false,"xtradb_cluster":true}
      }'

# Bob
curl -sS -X POST $BASE_URL/bootstrap/tenant \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Idempotency-Key: 22222222-2222-2222-2222-222222222222" \
  -H "Content-Type: application/json" \
  -d '{
        "username":"bob",
        "password":"AnotherP@ss1",
        "namespace":"ns-bob",
        "operators":{"postgresql":true,"mongodb":true,"xtradb_cluster":false}
      }'
```

Safe CLI wrappers (accounts)
```bash
# Create user
curl -sS -X POST $BASE_URL/cli/accounts/create \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice"}'

# Set password
curl -sS -X POST $BASE_URL/cli/accounts/set-password \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice","new_password":"NewP@ss"}'

# List users
curl -sS -X GET $BASE_URL/cli/accounts/list -H "X-Admin-Key: $ADMIN_API_KEY"

# Delete user
curl -sS -X DELETE $BASE_URL/cli/accounts \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice"}'
```

Safe CLI wrappers (namespaces)
```bash
# Add namespace (enable postgres operator only)
curl -sS -X POST $BASE_URL/cli/namespaces/add \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","operators":{"postgresql":false}}'

# Update namespace (enable mongodb)
curl -sS -X POST $BASE_URL/cli/namespaces/update \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","operators":{"mongodb":true}}'

# Remove namespace (keep K8s namespace)
curl -sS -X DELETE $BASE_URL/cli/namespaces/remove \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","keep_namespace":true}'
```

RBAC administration
```bash
# Append policy lines (atomic write + validate if APPLY_RBAC=true)
cat > /tmp/policy_lines.txt <<'EOF'
p, role:tenant-ns-alice, namespaces, read, ns-alice
p, role:tenant-ns-alice, database-engines, read, ns-alice/*
p, role:tenant-ns-alice, database-clusters, *, ns-alice/*
p, role:tenant-ns-alice, database-cluster-backups, *, ns-alice/*
p, role:tenant-ns-alice, database-cluster-restores, *, ns-alice/*
p, role:tenant-ns-alice, database-cluster-credentials, read, ns-alice/*
p, role:tenant-ns-alice, backup-storages, *, ns-alice/*
p, role:tenant-ns-alice, monitoring-instances, *, ns-alice/*
g, alice, role:tenant-ns-alice
EOF

curl -sS -X POST $BASE_URL/rbac/append \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d "$(jq -Rn --slurpfile lines /tmp/policy_lines.txt '{lines:$lines|.[0]|split("\n")|map(select(length>0))}')"

# Can check
curl -sS -X POST $BASE_URL/rbac/can \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"user":"alice","resource":"database-clusters","verb":"create","object":"ns-alice/*"}'
```

Per‑namespace limits
```bash
# Upsert limits for Alice
curl -sS -X PUT $BASE_URL/limits \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "namespace":"ns-alice",
        "max_clusters":3,
        "allowed_engines":["postgresql","mysql"],
        "cpu_limit_cores":4,
        "memory_limit_bytes":17179869184,
        "max_db_users":20
      }'

# Read limits
curl -sS -X GET $BASE_URL/limits/ns-alice -H "X-Admin-Key: $ADMIN_API_KEY"
```

Enforcement checks
```bash
# Validate headroom for a new cluster
curl -sS -X POST $BASE_URL/enforce/cluster-create \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","engine":"postgresql","cpu_request_cores":1,"memory_request_bytes":2147483648}'
```

Usage counters
```bash
# Register cluster create (increments usage)
curl -sS -X POST $BASE_URL/usage/register-cluster \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"create","cpu_cores":1,"memory_bytes":2147483648}'

# Register DB user create
curl -sS -X POST $BASE_URL/usage/register-db-user \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"create"}'

# Register cluster delete (decrements usage)
curl -sS -X POST $BASE_URL/usage/register-cluster \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"delete","cpu_cores":1,"memory_bytes":2147483648}'
```

Day‑2 operations
```bash
# Remove user (and later optionally remove RBAC via /rbac/append with a cleaned policy)
curl -sS -X DELETE $BASE_URL/users \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"bob"}'

# Remove namespace entirely (force=false path uses everestctl namespaces remove)
curl -sS -X DELETE $BASE_URL/namespaces \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","force":false,"keep_namespace":false}'

# Rotate password
curl -sS -X POST $BASE_URL/users/rotate-password \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice","new_password":"EvenStrongerP@ss2"}'
```

Admin views
```bash
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/tenants | jq .
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/audit | jq .
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/users/raw
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/namespaces/raw
```

CRD artifacts
```bash
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
