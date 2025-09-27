#!/usr/bin/env sh
set -eu

if [ -z "${ADMIN_API_KEY:-}" ]; then
  echo "ADMIN_API_KEY is required" >&2
  exit 1
fi

if ! command -v everestctl >/dev/null 2>&1; then
  echo "everestctl binary not found in PATH" >&2
  exit 1
fi

POLICY_FILE="${POLICY_FILE:-/var/lib/everest/policy/policy.csv}"
mkdir -p "$(dirname "$POLICY_FILE")"
touch "$POLICY_FILE"

SQLITE_DB="${SQLITE_DB:-/var/lib/everest/data/tenant_proxy.db}"
mkdir -p "$(dirname "$SQLITE_DB")"

# Ensure kubectl is present
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl binary not found in PATH" >&2
  exit 1
fi

# Export kubeconfig (mounted read-only by docker-compose)
export KUBECONFIG="${KUBECONFIG:-/data/kubeconfig}"
if [ ! -f "$KUBECONFIG" ]; then
  echo "Warning: KUBECONFIG file not found at $KUBECONFIG" >&2
fi

exec gunicorn -c gunicorn_conf.py app:app
