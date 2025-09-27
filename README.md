# Everestctl API (FastAPI wrapper)

Production-ready FastAPI service that wraps Percona Everest CLI (`everestctl`) to manage tenants (user + namespace + RBAC), rotate passwords, enforce quotas, and provide audit logs and metrics.

## Features

- Security: API key auth (constant-time check), in-memory rate limiting, safe subprocess execution, structured JSON logs with secret masking.
- Stability: timeouts and retries for CLI calls, atomic `policy.csv` updates with backups, graceful shutdown.
- Observability: `/metrics` (Prometheus), JSON logs with event names (`cli_run`, `rbac_change`, `http_request`).
- Ops Ready: `/healthz` and `/readyz`, audit logging (SQLite by default, optional Postgres via `DB_URL`), clear error handling.
- Deployment: Dockerfile (slim, non-root, tini), docker-compose, entrypoint that verifies `everestctl`, example `.env`.
- Tests: pytest with mocks for CLI; RBAC and quota unit tests.

> Fail-fast: The app refuses to start if `API_KEY` is missing. CLI presence is verified at startup unless `SKIP_CLI_CHECK=1` is set (useful for local tests).

## Quickstart (Docker Compose)

```bash
cp .env.example .env
# Edit .env and set API_KEY
docker compose up --build
```

The image already includes `kubectl` and `everestctl`.
You only need to provide a kubeconfig via the compose mount.

Kubeconfig: everestctl requires a kubeconfig. The compose files mount your host kubeconfig and set the appropriate environment so the CLI can use it. You can override the host path via `KUBECONFIG_HOST_PATH` in `.env`.

### Pinning tool versions at build time

By default, the Dockerfile installs `kubectl` at the latest stable release and `everestctl` at the latest GitHub release. You can pin or override them using build args:

```bash
docker build \
  --build-arg KUBECTL_VERSION=v1.30.4 \
  --build-arg EVERESTCTL_VERSION=v0.11.0 \
  --build-arg EVERESTCTL_REPO=percona/everest \
  -t ghcr.io/vaheed/everestctl-api:custom .
```

## Production Image (GHCR)

Pull the published image:

```bash
docker pull ghcr.io/vaheed/everestctl-api:latest
```

Or use the production compose file which references GHCR:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | required | Shared secret for `X-API-Key` header |
| `EVERESTCTL_PATH` | internal default | CLI path |
| `RBAC_POLICY_PATH` | `/data/policy.csv` | Policy file path |
| `DB_PATH` | `/data/audit.db` | SQLite DB path |
| `DB_URL` | empty | Optional Postgres URL (e.g., `postgresql://user:pass@host:5432/db`). If set, overrides SQLite and uses Postgres for audit/counters. |
| `RATE_LIMIT_PER_MIN` | `120` | Requests per minute |
| `RATE_LIMIT_BURST` | `150` | Burst tokens |
| `REQUEST_TIMEOUT_SEC` | `20` | CLI call timeout |
| `REQUEST_RETRIES` | `2` | CLI retries |
| `ALLOWED_ENGINES` | `postgres,mysql` | Allowed engines |
| `MAX_CLUSTERS_PER_TENANT` | `5` | Cluster quota per tenant |
| `METRICS_ENABLED` | `true` | Enable `/metrics` |
| `CORS_ALLOW_ORIGINS` | `*` | CORS origins |
| `HEALTH_STARTUP_PROBE_CMD` | `version` | Startup probe command |
| `WEB_CONCURRENCY` | `2` | Gunicorn workers |
| `GUNICORN_TIMEOUT` | `60` | Worker timeout |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Graceful timeout |
| `SKIP_CLI_CHECK` | `false` | Skip CLI presence check (tests/dev) |

## API

All endpoints require header: `X-API-Key: <your API key>`.

### Health

```bash
curl -s localhost:8080/healthz
curl -s localhost:8080/readyz | jq
```

### Create tenant

```bash
curl -X POST localhost:8080/tenants/create \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"user":"alice","namespace":"ns-alice","password":"S3cret!","engine":"postgres"}'
```

### Delete tenant

```bash
curl -X POST localhost:8080/tenants/delete \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"user":"alice","namespace":"ns-alice"}'
```

### Rotate password

```bash
curl -X POST localhost:8080/tenants/rotate-password \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"user":"alice","new_password":"NewS3cret!"}'
```

### Quota info

```bash
curl -s -H "X-API-Key: $API_KEY" localhost:8080/tenants/alice/quota | jq
```

### Metrics

```bash
curl -s localhost:8080/metrics
```

## Notes on `everestctl`

The app invokes `everestctl` using safe, argument-validated subprocess calls. Adjust the exact subcommands in `app.py` under the `create_tenant` / `delete_tenant` / `rotate_password` endpoints to match your CLI semantics.
If you need the CLI on your host, follow the official Everest documentation for installation steps appropriate to your platform.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export $(cat .env.example | xargs)  # set defaults
export API_KEY=dev-key
export SKIP_CLI_CHECK=1             # optional for local dev
uvicorn app:app --reload --port 8080
```

## Testing

```bash
pytest -q
```

## CI/CD

- CI runs tests and builds the Docker image on pushes and PRs.
- Docker images publish to GitHub Container Registry on pushes to `main` and tags. Pull via:

```bash
docker pull ghcr.io/<owner>/<repo>:<tag>
```

## JSON Logs

Events use `event` and `extras` fields, e.g.
- `http_request`
- `cli_run` (CLI args are masked for secrets)
- `rbac_change`
- `unhandled_exception`

## Data persistence

- `policy.csv` lives in the mounted volume.
- SQLite at `DB_PATH` stores audit logs and quota counters.

## License

MIT (example). Use at your own risk.
