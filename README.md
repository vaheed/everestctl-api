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
- The API manages per-tenant CRDs automatically during bootstrap and limit updates — no need to run kubectl manually.

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

Bootstrap cheat sheet
- Use Idempotency-Key header to safely retry.
- Operators: set booleans for `postgresql`, `mongodb`, `xtradb_cluster`.
- The bootstrap flow creates namespace, user, password, RBAC, CRD limits, and initializes counters.

Direct bootstrap (full example)
```bash
curl -sS -X POST $BASE_URL/bootstrap/tenant \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Idempotency-Key: 33333333-3333-3333-3333-333333333333" \
  -H "Content-Type: application/json" \
  -d '{
        "username": "charlie",
        "password": "S0meStr0ngP@ss",
        "namespace": "ns-charlie",
        "operators": {"postgresql": true, "mongodb": false, "xtradb_cluster": true}
      }'
# Example 200 OK response
# {"status":"ok","namespace":"ns-charlie","username":"charlie"}
```

Template-based bootstrap
1) Create a reusable template (includes defaults, operators, limits, extra RBAC)
```bash
curl -sS -X POST $BASE_URL/templates \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "name": "standard-tenant",
        "blueprint": {
          "defaults": {"username": "tenantuser", "password": "ChangeMeP@ss1", "namespace": "ns-tenant"},
          "operators": {"postgresql": true, "mongodb": false, "xtradb_cluster": false, "take_ownership": false},
          "limits": {"namespace":"ns-tenant","max_clusters":3,"allowed_engines":["postgresql","mysql"],"cpu_limit_cores":4,"memory_limit_bytes":17179869184,"max_db_users":20},
          "rbac_extra": [
            "p, role:tenant-{namespace}, backup-storages, create, {namespace}/*",
            "g, {username}, role:tenant-{namespace}"
          ]
        }
      }'
```

2) Bootstrap a tenant from the template, overriding identity and limits
```bash
curl -sS -X POST $BASE_URL/bootstrap/from-template \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Idempotency-Key: 44444444-4444-4444-4444-444444444444" \
  -H "Content-Type: application/json" \
  -d '{
        "template": "standard-tenant",
        "username": "dana",
        "password": "AnotherStrongP@ss",
        "namespace": "ns-dana",
        "operators": {"postgresql": true, "mongodb": true, "xtradb_cluster": false, "take_ownership": true},
        "limits": {"namespace":"ns-dana","max_clusters":5,"allowed_engines":["postgresql","mongodb"],"cpu_limit_cores":8,"memory_limit_bytes":34359738368,"max_db_users":40}
      }'
# Example 200 OK response
# {"status":"ok","namespace":"ns-dana","username":"dana","template":"standard-tenant"}
```
```

Accounts (API wrappers)
```bash
# Create user (Alice)
curl -sS -X POST $BASE_URL/cli/accounts/create \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice"}'

# Set password (Alice)
curl -sS -X POST $BASE_URL/cli/accounts/set-password \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice","new_password":"NewP@ss"}'

# List users (raw output)
curl -sS -X GET $BASE_URL/cli/accounts/list -H "X-Admin-Key: $ADMIN_API_KEY"

# Delete user (Alice)
curl -sS -X DELETE $BASE_URL/cli/accounts \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice"}'
```

Namespaces (API wrappers)
```bash
# Add namespace for Alice (enable postgres operator only, do not take ownership)
curl -sS -X POST $BASE_URL/cli/namespaces/add \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","operators":{"postgresql":false},"take_ownership":false}'

# Update namespace (enable mongodb for Alice)
curl -sS -X POST $BASE_URL/cli/namespaces/update \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","operators":{"mongodb":true}}'

# Remove namespace (keep K8s namespace for Alice)
curl -sS -X DELETE $BASE_URL/cli/namespaces/remove \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","keep_namespace":true}'
```

RBAC administration (all via API)
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

# Can check for Alice
curl -sS -X POST $BASE_URL/rbac/can \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"user":"alice","resource":"database-clusters","verb":"create","object":"ns-alice/*"}'

# Can check for Bob
curl -sS -X POST $BASE_URL/rbac/can \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"user":"bob","resource":"database-clusters","verb":"create","object":"ns-bob/*"}'
```

Per‑namespace limits (Alice and Bob)
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

# Read limits (Alice)
curl -sS -X GET $BASE_URL/limits/ns-alice -H "X-Admin-Key: $ADMIN_API_KEY"

# Upsert limits for Bob
curl -sS -X PUT $BASE_URL/limits \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{
        "namespace":"ns-bob",
        "max_clusters":5,
        "allowed_engines":["postgresql","mongodb"],
        "cpu_limit_cores":8,
        "memory_limit_bytes":34359738368,
        "max_db_users":40
      }'

# Read limits (Bob)
curl -sS -X GET $BASE_URL/limits/ns-bob -H "X-Admin-Key: $ADMIN_API_KEY"
```

Enforcement checks
```bash
# Validate headroom for a new cluster (Alice)
curl -sS -X POST $BASE_URL/enforce/cluster-create \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","engine":"postgresql","cpu_request_cores":1,"memory_request_bytes":2147483648}'

# Validate headroom for a new cluster (Bob)
curl -sS -X POST $BASE_URL/enforce/cluster-create \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","engine":"mongodb","cpu_request_cores":2,"memory_request_bytes":4294967296}'
```

Usage counters
```bash
# Register cluster create (increments usage for Alice)
curl -sS -X POST $BASE_URL/usage/register-cluster \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"create","cpu_cores":1,"memory_bytes":2147483648}'

# Register DB user create (Alice)
curl -sS -X POST $BASE_URL/usage/register-db-user \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"create"}'

# Register cluster delete (decrements usage for Alice)
curl -sS -X POST $BASE_URL/usage/register-cluster \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-alice","op":"delete","cpu_cores":1,"memory_bytes":2147483648}'

# Register cluster create (Bob)
curl -sS -X POST $BASE_URL/usage/register-cluster \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","op":"create","cpu_cores":2,"memory_bytes":4294967296}'

# Register DB user create (Bob)
curl -sS -X POST $BASE_URL/usage/register-db-user \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","op":"create"}'
```

Day‑2 operations
```bash
# Remove user (Alice)
curl -sS -X DELETE $BASE_URL/users \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"bob"}'

# Remove namespace entirely (Bob)
curl -sS -X DELETE $BASE_URL/namespaces \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","force":false,"keep_namespace":false}'

# Rotate password (Alice)
curl -sS -X POST $BASE_URL/users/rotate-password \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"alice","new_password":"EvenStrongerP@ss2"}'

# Rotate password (Bob)
curl -sS -X POST $BASE_URL/users/rotate-password \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"username":"bob","new_password":"AnotherP@ss2"}'
```

Admin views
```bash
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/tenants | jq .
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/audit | jq .
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/users/raw
curl -sS -H "X-Admin-Key: $ADMIN_API_KEY" $BASE_URL/namespaces/raw
```

Idempotency
- For POST/PUT/DELETE, set an Idempotency-Key header (UUID recommended) to safely retry without duplicating operations.
- Example header: `-H "Idempotency-Key: 33333333-3333-3333-3333-333333333333"`
# Add namespace for Bob (enable postgres and mongodb, take ownership)
curl -sS -X POST $BASE_URL/cli/namespaces/add \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","operators":{"postgresql":true,"mongodb":true,"xtradb_cluster":false},"take_ownership":true}'

# Remove namespace for Bob (delete K8s namespace)
curl -sS -X DELETE $BASE_URL/cli/namespaces/remove \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"namespace":"ns-bob","keep_namespace":false}'
