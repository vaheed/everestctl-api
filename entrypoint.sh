#!/usr/bin/env sh
set -eu

# Fail fast on missing API key
if [ -z "${API_KEY:-}" ]; then
  echo "ERROR: API_KEY is not set"; exit 1
fi

EVERESTCTL_PATH="${EVERESTCTL_PATH:-/usr/local/bin/everestctl}"
if [ "${SKIP_CLI_CHECK:-}" = "1" ] || [ "${SKIP_CLI_CHECK:-}" = "true" ]; then
  echo "INFO: skipping everestctl check (SKIP_CLI_CHECK set)"
else
  if [ ! -x "$EVERESTCTL_PATH" ] && ! command -v "$EVERESTCTL_PATH" >/dev/null 2>&1; then
    echo "ERROR: everestctl not found at $EVERESTCTL_PATH"; exit 1
  fi
  # Kubeconfig must be provided for CLI operations
  if [ -z "${KUBECONFIG:-}" ]; then
    echo "ERROR: KUBECONFIG is not set; mount your kubeconfig and set KUBECONFIG"; exit 1
  fi
  if [ ! -r "$KUBECONFIG" ]; then
    echo "ERROR: kubeconfig not found or not readable at $KUBECONFIG"; exit 1
  fi
fi

# Prepare data dir and files
mkdir -p /data
: "${RBAC_POLICY_PATH:=/data/policy.csv}"
: "${DB_PATH:=/data/audit.db}"
DB_URL=${DB_URL:-}
touch "$RBAC_POLICY_PATH"

# Initialize DB if needed (app also ensures schema)
if [ -z "$DB_URL" ]; then
python - <<'PY'
import os, sqlite3
db = os.environ.get("DB_PATH","/data/audit.db")
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, details TEXT NOT NULL);")
conn.execute("CREATE TABLE IF NOT EXISTS counters(tenant TEXT PRIMARY KEY, clusters INTEGER NOT NULL DEFAULT 0);")
conn.commit(); conn.close()
PY
else
  echo "INFO: DB_URL set; skipping local SQLite init"
fi

# Smoke check everestctl (best-effort)
"$EVERESTCTL_PATH" version >/dev/null 2>&1 || true

exec gunicorn -c gunicorn_conf.py app:app
