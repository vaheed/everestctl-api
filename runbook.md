
# VirakCloud × Percona Everest — Tenant Isolation & API Runbook (Alice & Bob)

> **Scope**
> - **A) One-time CLI (per user/tenant)** — namespace + user + **mandatory RBAC**
> - **B) Regular REST API calls** — login, create DB, list, creds, scale, backup, delete  
> Sources: official Percona Everest docs (see inline citations).

---

## A) One-time CLI (per user/tenant)

**Goal:** create an **isolated namespace** per tenant, a **local user** (if you don’t use SSO), and **RBAC** so the user can only operate inside *their* namespace.

### 1) Add a managed DB namespace for the tenant
> Docs: Namespaces management. (citeturn0search1)

**Alice → `ns-alice`**
```bash
everestctl namespaces add ns-alice   --operator.postgresql=true   --operator.mongodb=false   --operator.xtradb-cluster=true
```

**Bob → `ns-bob`**
```bash
everestctl namespaces add ns-bob   --operator.postgresql=true   --operator.mongodb=false   --operator.xtradb-cluster=true
```

> Registers the namespace with Everest and installs selected DB operators in that namespace. (citeturn0search1)

### 2) Create a local Everest user (skip if using SSO)
> Docs: API overview & auth; local accounts via CLI. (citeturn0search2)

**Alice user**
```bash
everestctl accounts create -u alice
everestctl accounts set-password -u alice
```

**Bob user**
```bash
everestctl accounts create -u bob
everestctl accounts set-password -u bob
```

### 3) Enable RBAC and define **per-tenant** permissions
> Docs: RBAC guide & policy.csv format. (citeturn0search0)

Edit the ConfigMap:
```bash
kubectl edit configmap everest-rbac -n everest-system
```

Minimal, safe **per-tenant** policy for Alice and Bob (replace passwords only; names kept literal for clarity):

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: everest-rbac
  namespace: everest-system
data:
  enabled: "true"
  policy.csv: |
    # ---------- Alice: limit to ns-alice ----------
    p, role:tenant-ns-alice, namespaces, read, ns-alice
    p, role:tenant-ns-alice, database-engines, read, ns-alice/*
    p, role:tenant-ns-alice, database-clusters, *, ns-alice/*
    p, role:tenant-ns-alice, database-cluster-credentials, read, ns-alice/*
    p, role:tenant-ns-alice, database-cluster-backups, *, ns-alice/*
    p, role:tenant-ns-alice, database-cluster-restores, *, ns-alice/*
    p, role:tenant-ns-alice, backup-storages, *, ns-alice/*
    p, role:tenant-ns-alice, monitoring-instances, *, ns-alice/*

    g, alice, role:tenant-ns-alice

    # ---------- Bob: limit to ns-bob ----------
    p, role:tenant-ns-bob, namespaces, read, ns-bob
    p, role:tenant-ns-bob, database-engines, read, ns-bob/*
    p, role:tenant-ns-bob, database-clusters, *, ns-bob/*
    p, role:tenant-ns-bob, database-cluster-credentials, read, ns-bob/*
    p, role:tenant-ns-bob, database-cluster-backups, *, ns-bob/*
    p, role:tenant-ns-bob, database-cluster-restores, *, ns-bob/*
    p, role:tenant-ns-bob, backup-storages, *, ns-bob/*
    p, role:tenant-ns-bob, monitoring-instances, *, ns-bob/*

    g, bob, role:tenant-ns-bob
```

> **Why per-namespace rules?** Since **v1.2+**, `backup-storages` and `monitoring-instances` are **namespace-scoped**, and a DB cluster can reference only same-namespace resources. (citeturn0search4)

(Optional) Validate policy:
```bash
everestctl settings rbac validate
everestctl settings rbac can --policy-file <optional_file>
```
(citeturn0search0)

---

## B) Regular REST API calls (tenant or your backend)

> All calls: `Authorization: Bearer <JWT>`. API overview and endpoints: (citeturn0search2)

### 0) Login → get JWT (per session)
> `/v1/session`, default rate limit ~3 RPS; configurable. (citeturn0search5)
```bash
curl -s -X POST "https://<EVEREST_HOST>/v1/session"   -H "Content-Type: application/json"   -d '{"username":"alice","password":"<ALICE_PASSWORD>"}'
# → { "token": "eyJhbGciOi..." }
```

Repeat for Bob:
```bash
curl -s -X POST "https://<EVEREST_HOST>/v1/session"   -H "Content-Type: application/json"   -d '{"username":"bob","password":"<BOB_PASSWORD>"}'
```

### Namespace bootstrap (run **once** per tenant namespace via API)
> Must live **in the same namespace** as the DBs. (citeturn0search4)

**1) Create backup storage (S3/MinIO)** (Alice)
```bash
TOKEN="<ALICE_JWT>"
curl -s -X POST "https://<EVEREST_HOST>/v1/namespaces/ns-alice/backup-storages"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{
        "name": "alice-s3",
        "type": "s3",
        "endpoint": "https://s3.example.com",
        "bucket": "everest-alice",
        "region": "us-east-1",
        "credentialsSecret": "alice-s3-creds"
      }'
```
(citeturn0search4)

**2) Create monitoring instance (PMM)** (Alice)
```bash
curl -s -X POST "https://<EVEREST_HOST>/v1/namespaces/ns-alice/monitoring-instances"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{
        "name": "alice-pmm",
        "type": "pmm",
        "endpoint": "https://pmm.alice.example.com"
      }'
```
(citeturn0search16)

Repeat the two calls for **Bob** using `ns-bob` and `BOB_JWT`.

---

### Database lifecycle (repeatable)

**Create a database cluster** (Alice → PostgreSQL)
> Endpoint and body pattern: CreateDatabaseCluster. (citeturn0search2)
```bash
TOKEN="<ALICE_JWT>"
curl -s -X POST "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{
        "apiVersion": "everest.percona.com/v1alpha1",
        "kind": "DatabaseCluster",
        "metadata": { "name": "pg-alice" },
        "spec": {
          "engine": {
            "type": "postgresql",
            "version": "17.4",
            "replicas": 1,
            "storage": { "size": "25Gi", "class": "standard-rwo" },
            "resources": { "cpu": "1", "memory": "2Gi" }
          },
          "proxy": { "type": "pgbouncer", "replicas": 1, "expose": { "type": "external" } },
          "backup": { "pitr": { "enabled": false } },
          "monitoring": {}
        }
      }'
```

**List clusters** (Alice)
```bash
curl -s "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters"   -H "Authorization: Bearer $TOKEN"
```

**Get cluster (poll for readiness)** (Alice)
```bash
curl -s "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice"   -H "Authorization: Bearer $TOKEN"
```

**Get connection credentials** (Alice)
```bash
curl -s "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice/credentials"   -H "Authorization: Bearer $TOKEN"
```
(citeturn0search11)

**Scale / change plan** (Alice — increase replicas)
```bash
curl -s -X PUT "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{
        "spec": {
          "engine": { "replicas": 3 }
        }
      }'
```
(citeturn0search12)

**Delete cluster** (Alice)
```bash
curl -s -X DELETE "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice"   -H "Authorization: Bearer $TOKEN"
```

> Repeat the same CRUD calls for **Bob** by swapping `ns-bob`, `pg-bob`, and `BOB_JWT`.

---

### Backups & restores

> Requires the **backup storage** created earlier in the same namespace. (citeturn0search4)

**Create on-demand backup** (Alice)
```bash
curl -s -X POST "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice/backups"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{ "name": "backup-2025-09-25-01", "storageName": "alice-s3" }'
```
(citeturn0search15)

**List backups** (Alice)
```bash
curl -s "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice/backups"   -H "Authorization: Bearer $TOKEN"
```

**Restore from backup** (Alice → new target `pg-alice-restore`)
```bash
curl -s -X POST "https://<EVEREST_HOST>/v1/namespaces/ns-alice/database-clusters/pg-alice-restore/restores"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{ "sourceBackupName": "backup-2025-09-25-01" }'
```

> Use equivalent backup/restore calls for **Bob** with `ns-bob` values.

---

## Notes & References

- **API overview / entrypoint** (endpoints incl. `/v1/session`, database clusters, credentials, etc.). (citeturn0search2)
- **Namespaces management** (`everestctl namespaces add`). (citeturn0search1)
- **RBAC** (enablement, policy.csv format, role→user bindings). (citeturn0search0)
- **Breaking change**: backup-storages & monitoring-instances are **namespace-scoped**; clusters may use only same-namespace resources. (citeturn0search4)
- **Session rate limiting** for `/v1/session`. (citeturn0search5)
- **Proxy & scaling features** examples in release notes. (citeturn0search12)
- **API demo** showing create/list/get/credentials/delete flows. (citeturn0search11)

---

### TL;DR

1) **CLI once per tenant:** `everestctl namespaces add ns-<TENANT>` → `everestctl accounts create/set-password` → update `everest-rbac` ConfigMap (role per namespace + `g, user, role`). (citeturn0search1turn0search0)  
2) **API bootstrap per namespace:** `POST /namespaces/{ns}/backup-storages` → `POST /namespaces/{ns}/monitoring-instances`. (citeturn0search4)  
3) **Daily API:** create/list/get/credentials/scale/backup/restore/delete DB clusters in that namespace. (citeturn0search2)
