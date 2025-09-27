# Everestctl Async API (FastAPI)

Production-ready, minimal async API to bootstrap users and namespaces in a Percona Everest cluster using `everestctl` and `kubectl`. Implements an async job pattern with polling and structured results.

- Async FastAPI + in-memory job store (documented upgrade path)
- Endpoints secured by `X-Admin-Key`
- Docker image installs `kubectl` and `everestctl`
- docker-compose mounts host kubeconfig
- GitHub Actions: test → build/push

## Quickstart

```bash
docker-compose up --build -d
export BASE_URL="http://localhost:8080"
export ADMIN_API_KEY="changeme"
```

## Submit a bootstrap job
```bash
curl -sS -X POST "$BASE_URL/bootstrap/users" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "username": "alice" }'
# => { "job_id": "...", "status_url": "/jobs/..." }
```

## Check job
```bash
curl -sS "$BASE_URL/jobs/<JOB_ID>" -H "X-Admin-Key: $ADMIN_API_KEY"
```

## Get result
```bash
curl -sS "$BASE_URL/jobs/<JOB_ID>/result" -H "X-Admin-Key: $ADMIN_API_KEY"
```

## List accounts
```bash
curl -sS -X GET "$BASE_URL/accounts/list" -H "X-Admin-Key: $ADMIN_API_KEY"
```

Notes
- Works async: submit → poll → fetch result.
- Container expects kubeconfig mounted at `/root/.kube/config`.

## Endpoints

- POST `/bootstrap/users` → 202 with `{ job_id, status_url }`
- GET `/jobs/{job_id}` → job status
- GET `/jobs/{job_id}/result` → final structured result
- GET `/accounts/list` → pass-through to `everestctl account list`, returns JSON or parsed table
- GET `/healthz`, `/readyz` → basic probes

Auth: all protected endpoints require header `X-Admin-Key: <ADMIN_API_KEY>`.

## Configuration

- `ADMIN_API_KEY`: shared header token for admin actions
- `SUBPROCESS_TIMEOUT` (default 60): per-command timeout seconds
- `MAX_OUTPUT_CHARS` (default 20000): truncate large outputs
- `EVEREST_RBAC_APPLY_CMD`: if set, apply Casbin-like policy using this command. Supports `{file}` placeholder or appends `--file <path>`.

## Implementation Highlights

- In-memory job queue with asyncio tasks. For production, switch to Redis/RQ, Celery, or DB queue with persistence and retries.
- Structured logging to stdout with request id and timing via middleware.
- ResourceQuota and LimitRange YAMLs generated from inputs and applied via `kubectl apply -f -`.
- Error handling maps CLI failures to 502 on pass-through and marks jobs failed with clear summaries.

## Docker

Build and run locally:
```bash
docker build -t everestctl-api .
docker run --rm -p 8080:8080 -e ADMIN_API_KEY=changeme \
  -v $HOME/.kube/config:/root/.kube/config:ro everestctl-api
```

## CI

- GitHub Actions runs tests, then builds and optionally pushes an image if registry secrets exist.

## Development

- Install deps: `pip install -r requirements.txt`
- Run API: `uvicorn app.app:app --host 0.0.0.0 --port 8080`
- Run tests: `pytest -q`

## Security Notes

- Header auth enforced for all mutating and protected routes.
- Do not log secrets; logs contain minimal request metadata and a correlation id.
- Container runs as non-root (UID 10001) but may require root if CLIs demand it; adjust Dockerfile accordingly.

## References

- Percona Everest docs: https://docs.percona.com/everest/

