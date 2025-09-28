# Everest Bootstrap Guide (everestctl)

End-to-end steps to bootstrap a user and namespace in Percona Everest using everestctl and kubectl: create user → create namespace → install CRDs/operators for that namespace → bind user to namespace with RBAC.

## 0) Prerequisites
- Kube access configured: `export KUBECONFIG=~/.kube/config` (or your path)
- Tools on PATH: `kubectl`, `everestctl`
- Verify:
  - `kubectl version --client --short`
  - `kubectl config current-context`
  - `everestctl --version` or `everestctl version`

Install everestctl
- Linux/WSL: `curl -sSL -o everestctl-linux-amd64 https://github.com/percona/everest/releases/latest/download/everestctl-linux-amd64 && sudo install -m 555 everestctl-linux-amd64 /usr/local/bin/everestctl && rm everestctl-linux-amd64`

## 1) Create a user
- Create user (prompts for password):
  - `everestctl accounts create -u <username>`
- Alternatively set/reset password:
  - `everestctl accounts set-password -u <username>`
- List users (supports `--json` on many versions):
  - `everestctl accounts list`

Admin account quickstart
- Retrieve initial admin password (if you need to log into the UI/API as admin):
  - `everestctl accounts initial-admin-password`
- Immediately rotate the admin password after first login:
  - `everestctl accounts set-password -u admin`

 

## 2) Create a namespace and install operators/CRDs
- Provision namespace and select operators for it:
  - `everestctl namespaces add <namespace> \`
    `  --operator.mongodb=<true|false> \`
    `  --operator.postgresql=<true|false> \`
    `  --operator.xtradb-cluster=<true|false> \`
    `  [--take-ownership]`

Notes
- `--take-ownership` lets Everest adopt an existing Kubernetes namespace.
- Depending on version, `--operator.mysql` replaces `--operator.xtradb-cluster` (xtradb flag is deprecated in newer docs). Use the flag supported by your everestctl.
- When a namespace is added with an operator enabled, everestctl ensures the operator and its CRDs are installed/available in the cluster.

Validate namespace and CRDs
- Namespace exists: `kubectl get namespace <namespace>`
- CRDs present (generic checks):
  - `kubectl get crd | grep -i percona || true`
  - `kubectl get crd | grep -Ei 'psmdb|pxc|postgres|pg|percona' || true`

Update operators later (add only):
- `everestctl namespaces update <namespace>`

Remove namespace and managed resources:
- `everestctl namespaces remove <namespace> [--keep-namespace]`

 

## 3) Bind the user to the namespace (RBAC)
RBAC is disabled by default and configured via the `everest-rbac` ConfigMap in `everest-system`. Policies use a Casbin-like syntax.

Policy grammar
- `p, <subject>, <resource-type>, <action>, <resource-name>`
- Assign user to role: `g, <username>, role:<role-name>`

Example: Give user `<username>` full control within `<namespace>` by creating a role named `role:<username>` and binding the user to it.

Apply via ConfigMap
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: everest-rbac
  namespace: everest-system
data:
  enabled: "true"
  policy.csv: |
    # role with full access within a single namespace
    p, role:<username>, namespaces, read, <namespace>
    # IMPORTANT: engines must be readable across all to create DBs
    p, role:<username>, database-engines, read, */*
    p, role:<username>, database-clusters, *, <namespace>/*
    p, role:<username>, database-cluster-backups, *, <namespace>/*
    p, role:<username>, database-cluster-restores, *, <namespace>/*
    p, role:<username>, database-cluster-credentials, read, <namespace>/*
    p, role:<username>, backup-storages, *, <namespace>/*
    p, role:<username>, monitoring-instances, *, <namespace>/*
    # bind user to the role
    g, <username>, role:<username>
```
Apply:
- `kubectl apply -f everest-rbac-config.yaml` (or using stdin: `kubectl apply -f - <<'EOF' ... EOF`)

Validate:
- `kubectl get configmap everest-rbac -n everest-system -o yaml`

Alternative apply path (if supported by your everestctl)
- Some versions provide a CLI import, e.g.: `everestctl access-control import --file policy.csv`
- If using automation (like this repo’s API), set an env such as `EVEREST_RBAC_APPLY_CMD="everestctl access-control import --file {file}"` to apply policies programmatically.

 

## 4) Optional: Set quotas/limits for the namespace
While not strictly part of user binding, many teams cap usage per namespace.

Apply a ResourceQuota and LimitRange (example; adjust values):
```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: user-quota
  namespace: <namespace>
spec:
  hard:
    requests.cpu: "2"
    requests.memory: "2048Mi"
    requests.storage: "20Gi"
    limits.cpu: "2"
    limits.memory: "2048Mi"
---
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: <namespace>
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: "1"
      memory: "512Mi"
    default:
      cpu: "1"
      memory: "1024Mi"
```
Apply: `kubectl apply -n <namespace> -f quota-limits.yaml`

## Who can and cannot create databases
- RBAC disabled (default): all authenticated local users can create databases until you enable RBAC.
- RBAC enabled: a user can create databases only if ALL of the following are true:
  - Namespaces read permission is granted: `p, <role>, namespaces, read, *` (required for all roles)
  - Database engines read permission is granted for all engines: `p, <role>, database-engines, read, */*` (must be read-all to create)
  - Database clusters create permission in the target namespace: `p, <role>, database-clusters, create, <namespace>/*`
  - The namespace is managed by Everest and the relevant operator for the chosen engine is enabled for that namespace

Users who lack any of the above cannot create databases. For example, if `database-engines` read is scoped too narrowly (not `*/*`), creation will fail in the UI/API.

Limitations and prerequisites affecting creation
- Operators/CRDs: the engine operator must be installed/enabled for the namespace via `everestctl namespaces add/update`.
- Quotas: `ResourceQuota` or `LimitRange` may block scheduling if requests/limits exceed caps; adjust quota or requested size.
- Storage classes: a default or specified StorageClass must exist; otherwise volumes may fail to provision.
- Credentials visibility: without `database-cluster-credentials, read`, the user can create but won’t see generated credentials.
- Removing operators via `namespaces update` is not supported; you can add but not remove operators.

## Troubleshooting quick checks
- Kube access: `kubectl config current-context` and `kubectl get nodes`
- everestctl can reach cluster: run `everestctl version` and `everestctl accounts list`
- Namespace readiness: `kubectl get ns <namespace>` and operator pods in relevant namespaces
- RBAC active: ensure `everest-rbac` ConfigMap exists with `enabled: "true"` and your `policy.csv`

## Notes and version diffs
- `--operator.xtradb-cluster` is deprecated in newer versions; use `--operator.mysql` if your everestctl supports it.
- Some commands support `--json`. If `everestctl accounts list --json` fails, rerun without `--json`.
- CLI subcommand naming may vary across versions (`accounts` vs `account`). Prefer plural.

## Personalized, ready-to-apply snippets
Set your variables once, then copy/paste each block.

```bash
# REQUIRED: customize these
export USERNAME="alice"
export NAMESPACE="alice-ns"

# Optional sizing for quota/limits
export CPU_CORES=2
export RAM_MB=2048
export DISK_GB=20

# Choose operators for this namespace (true/false)
export MONGODB=true
export POSTGRESQL=true
# If your everestctl supports --operator.mysql (newer), set MYSQL
export MYSQL=false
# If your everestctl uses --operator.xtradb-cluster (older), set XTRADB_CLUSTER
export XTRADB_CLUSTER=false
```

1) Create the user
```bash
everestctl accounts create -u "$USERNAME" || everestctl accounts set-password -u "$USERNAME"
```

2) Create the namespace and enable operators/CRDs
```bash
# Prefer this (newer everestctl)
everestctl namespaces add "$NAMESPACE" \
  --operator.mongodb="$MONGODB" \
  --operator.postgresql="$POSTGRESQL" \
  --operator.mysql="$MYSQL" \
  || true
```

```bash
# Fallback for older everestctl (use one of the two blocks, not both)
everestctl namespaces add "$NAMESPACE" \
  --operator.mongodb="$MONGODB" \
  --operator.postgresql="$POSTGRESQL" \
  --operator.xtradb-cluster="$XTRADB_CLUSTER"

# Validate
kubectl get namespace "$NAMESPACE"
kubectl get crd | grep -Ei 'percona|psmdb|pxc|postgres|pg' || true
```

Accessing the Everest UI/API (choose one approach)
- LoadBalancer (if your cluster supports it):
  - Set: `helm upgrade --install everest percona/everest -n everest-system --set service.type=LoadBalancer`
  - Get external IP: `kubectl get svc/everest -n everest-system`
- Ingress: enable per your controller and DNS/TLS config (chart values: `ingress.enabled=true`)
- NodePort: `kubectl patch svc/everest -n everest-system -p '{"spec": {"type": "NodePort"}}'`
- Port-forward (local only): `kubectl port-forward svc/everest 8080:8080 -n everest-system`

3) Bind the user to the namespace (RBAC)
```bash
# Build policy.csv for this user/namespace
cat > policy.csv <<EOF
p, role:${USERNAME}, namespaces, read, ${NAMESPACE}
# engines must be readable across all to enable DB creation
p, role:${USERNAME}, database-engines, read, */*
p, role:${USERNAME}, database-clusters, *, ${NAMESPACE}/*
p, role:${USERNAME}, database-cluster-backups, *, ${NAMESPACE}/*
p, role:${USERNAME}, database-cluster-restores, *, ${NAMESPACE}/*
p, role:${USERNAME}, database-cluster-credentials, read, ${NAMESPACE}/*
p, role:${USERNAME}, backup-storages, *, ${NAMESPACE}/*
p, role:${USERNAME}, monitoring-instances, *, ${NAMESPACE}/*
g, ${USERNAME}, role:${USERNAME}
EOF

# Create/Update the ConfigMap with RBAC enabled and the policy
kubectl -n everest-system create configmap everest-rbac \
  --from-literal=enabled="true" \
  --from-file=policy.csv=policy.csv \
  -o yaml --dry-run=client | kubectl apply -f -

kubectl get configmap everest-rbac -n everest-system -o yaml | sed -n '1,120p'
```

4) Apply ResourceQuota and LimitRange to the namespace (optional)
```bash
cat > quota-limits.yaml <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: user-quota
  namespace: ${NAMESPACE}
spec:
  hard:
    requests.cpu: "${CPU_CORES}"
    requests.memory: "${RAM_MB}Mi"
    requests.storage: "${DISK_GB}Gi"
    limits.cpu: "${CPU_CORES}"
    limits.memory: "${RAM_MB}Mi"
---
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: ${NAMESPACE}
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: "1"
      memory: "512Mi"
    default:
      cpu: "1"
      memory: "1024Mi"
EOF

kubectl apply -n "$NAMESPACE" -f quota-limits.yaml
kubectl describe resourcequota user-quota -n "$NAMESPACE" || true
```

