import os
import shutil
import subprocess
from typing import Optional

DEFAULT_TIMEOUT_SECONDS = 20


def get_admin_api_key() -> Optional[str]:
    return os.getenv("ADMIN_API_KEY")


def validate_admin_key(provided: Optional[str], expected: Optional[str]) -> bool:
    if expected is None or expected == "":
        # If not configured, treat as invalid for safety.
        return False
    return provided is not None and provided == expected


def everestctl_available() -> bool:
    return shutil.which("everestctl") is not None


def run_everestctl_account_list(timeout: int = DEFAULT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    # Runs the command and returns the CompletedProcess. Raises CalledProcessError on failure.
    return subprocess.run(
        ["everestctl", "account", "list"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

