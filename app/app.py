import os
import subprocess
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from .dependencies import (
    everestctl_available,
    get_admin_api_key,
    run_everestctl_account_list,
    validate_admin_key,
)
from .parsers import parse_everestctl_output


app = FastAPI(title="everestctl-api", version="1.0.0")

# Evaluate availability at startup; tests can monkeypatch this.
EVERESTCTL_AVAILABLE = everestctl_available()


@app.get("/accounts/list")
def accounts_list(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    expected_key = get_admin_api_key()

    if not validate_admin_key(x_admin_key, expected_key):
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    if not EVERESTCTL_AVAILABLE:
        return JSONResponse(
            status_code=500,
            content={
                "error": "everestctl not found",
                "detail": "The 'everestctl' binary is not available on PATH. Ensure it is installed in the container and PATH is set.",
            },
        )

    try:
        cp: subprocess.CompletedProcess = run_everestctl_account_list()
    except subprocess.CalledProcessError as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": "everestctl failed",
                "detail": e.stderr or str(e),
            },
        )
    except subprocess.TimeoutExpired as e:
        return JSONResponse(
            status_code=502,
            content={
                "error": "everestctl timeout",
                "detail": str(e),
            },
        )

    parsed = parse_everestctl_output(cp.stdout)
    return {"data": parsed, "source": "everestctl account list"}


def get_port() -> int:
    raw = os.getenv("PORT", "8080")
    try:
        return int(raw)
    except ValueError:
        return 8080

