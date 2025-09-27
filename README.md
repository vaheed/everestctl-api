# everestctl-api

Minimal FastAPI service exposing `everestctl account list` over HTTP with auth, containerization, tests, and CI.

## Overview

- Endpoint: `GET /accounts/list`
- Auth: requires header `X-Admin-Key: <ADMIN_API_KEY>`
- On success: runs `everestctl account list`, parses stdout (JSON pass-through or robust text-table parsing), and returns:
  `{ "data": <parsed_result>, "source": "everestctl account list" }`
- On auth failure: `401 {"error":"unauthorized"}`
- On CLI failure: `502 {"error":"everestctl failed", "detail":"..."}`
- On missing everestctl: `500 {"error":"everestctl not found", ...}`

The service binds to `0.0.0.0` and uses `PORT` env var (default 8080).

## Quickstart

Prerequisites:

- Docker and docker-compose
- A valid kubeconfig on the host at `~/.kube/config`

Steps:

1. Build and start

   - `docker-compose up --build`

2. Usage

   - Export variables and call the API:

     ```sh
     export BASE_URL="http://localhost:8080"
     export ADMIN_API_KEY="changeme"
     curl -sS -X GET "$BASE_URL/accounts/list" -H "X-Admin-Key: $ADMIN_API_KEY"
     ```

## Configuration

- `ADMIN_API_KEY` is required for authorization and is validated against the `X-Admin-Key` header.
- `PORT` (default `8080`) controls the bind port.
- `KUBECONFIG` is expected to be `/root/.kube/config` inside the container; compose mounts the host kubeconfig there.

## Notes on everestctl

- The Dockerfile installs `kubectl` and attempts to install `everestctl`.
- Update the `EVERESTCTL_URL` in the Dockerfile to the correct Linux amd64 release URL if `everestctl` is a downloadable binary.
- If `everestctl` is a Python package, the Dockerfile includes a fallback `pip install everestctl` step (commented behavior explained inline) — adjust as needed.
- The container relies on a mounted kubeconfig at `/root/.kube/config` to operate with `kubectl`/`everestctl` using `KUBECONFIG` set.

## Project Layout

```
.
├── app/
│   ├── app.py                 # FastAPI app + route
│   ├── __init__.py
│   ├── dependencies.py        # auth helpers, subprocess wrappers
│   └── parsers.py             # parse everestctl output (JSON or text)
├── tests/
│   └── test_app.py            # pytest with subprocess mocking
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml             # test + deploy stages on push
└── README.md
```

## Development & Testing

Local tests:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

The tests mock `subprocess.run` to simulate `everestctl` output in JSON and tabular forms, and error scenarios.

## CI/CD

GitHub Actions workflow has two stages:

1. test
   - Checks out code, sets up Python 3.11, caches pip, installs requirements, runs `pytest -q`.
2. deploy (needs: test)
   - Builds a Docker image tagged with `${{ github.sha }}`.
   - Pushes the image to a registry if the following secrets are present: `REGISTRY`, `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`, `IMAGE_NAME`.
   - Includes a placeholder `kubectl apply` step that runs only if `KUBECONFIG_B64` and other registry secrets are present. Replace with your deployment commands.

Required secrets for deploy:

- `REGISTRY` (e.g., `ghcr.io/owner/repo` or `registry.hub.docker.com`)
- `REGISTRY_USERNAME`
- `REGISTRY_PASSWORD`
- `IMAGE_NAME` (e.g., `everestctl-api`)

Adjust registry/name as needed in `.github/workflows/ci.yml`.

## Troubleshooting

- 401 unauthorized: Ensure you send `X-Admin-Key` and it matches `ADMIN_API_KEY`.
- 500 everestctl not found: Validate that `everestctl` is installed in the container and on `PATH`. The Dockerfile includes installation steps — verify the URL or package installation.
- 502 everestctl failed: Inspect the `detail` field for stderr from the command; ensure `KUBECONFIG` is valid and the process has necessary cluster access.
- Missing kubeconfig: Ensure your host `~/.kube/config` exists; docker-compose mounts it read-only to `/root/.kube/config`.

## Security

- The service never logs `ADMIN_API_KEY`.
- Authorization enforced per request via `X-Admin-Key`.

