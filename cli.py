import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


ALLOWED_BASE = {
    ("everestctl", "accounts", "create"),
    ("everestctl", "accounts", "set-password"),
    ("everestctl", "accounts", "list"),
    ("everestctl", "accounts", "delete"),
    ("everestctl", "namespaces", "add"),
    ("everestctl", "namespaces", "update"),
    ("everestctl", "namespaces", "remove"),
    ("everestctl", "namespaces", "list"),
    ("everestctl", "settings", "rbac", "validate"),
    ("everestctl", "settings", "rbac", "can"),
    ("everestctl", "version"),
    ("everestctl", "help"),
}


def _is_allowed(argv: List[str]) -> bool:
    # Allow only exact prefixes listed
    for allowed in ALLOWED_BASE:
        if tuple(argv[: len(allowed)]) == allowed:
            return True
    return False


def _mask(argv: List[str]) -> List[str]:
    masked = []
    secret_next = False
    for x in argv:
        if secret_next:
            masked.append("****")
            secret_next = False
            continue
        if x in {"-p", "--password", "--new-password", "--token"}:
            masked.append(x)
            secret_next = True
        elif x.startswith("password="):
            masked.append("password=****")
        else:
            masked.append(x)
    return masked


def run(
    argv: List[str],
    timeout: Optional[int] = None,
    retries: Optional[int] = None,
    stdin_input: Optional[str] = None,
    use_pty: bool = False,
) -> Tuple[int, str, str]:
    if not argv or argv[0] != "everestctl":
        raise ValueError("must invoke everestctl")
    if not _is_allowed(argv):
        raise ValueError(f"command not allow-listed: {argv[:4]}")

    bin_path = os.environ.get("EVERESTCTL_BIN", "everestctl")
    argv = [bin_path] + argv[1:]
    timeout = int(os.environ.get("CLI_TIMEOUT_SEC", timeout or 30))
    retries = int(os.environ.get("CLI_RETRIES", retries or 2))

    last_code, last_out, last_err = 1, "", ""
    attempt = 0
    while attempt <= retries:
        attempt += 1
        start = time.time()
        try:
            if use_pty:
                import os
                import select

                master, slave = os.openpty()
                try:
                    proc = subprocess.Popen(
                        argv,
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        close_fds=True,
                        text=False,
                    )
                    os.close(slave)
                    out_chunks: List[str] = []
                    start_wait = time.time()
                    # We generally do not need to send stdin for create; if provided, write it.
                    if stdin_input:
                        os.write(master, stdin_input.encode())
                    while True:
                        if timeout and (time.time() - start_wait) > timeout:
                            proc.kill()
                            code, out, err = 124, "", f"timeout after {timeout}s"
                            break
                        r, _, _ = select.select([master], [], [], 0.1)
                        if r:
                            try:
                                data = os.read(master, 8192)
                            except OSError:
                                data = b""
                            if data:
                                out_chunks.append(data.decode(errors="ignore"))
                        if proc.poll() is not None:
                            # Drain remaining
                            try:
                                while True:
                                    data = os.read(master, 8192)
                                    if not data:
                                        break
                                    out_chunks.append(data.decode(errors="ignore"))
                            except OSError:
                                pass
                            code = proc.returncode
                            out = "".join(out_chunks)
                            err = ""
                            break
                finally:
                    try:
                        os.close(master)
                    except Exception:
                        pass
            else:
                proc = subprocess.run(
                    argv,
                    input=stdin_input,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                    text=True,
                )
                code = proc.returncode
                out = proc.stdout
                err = proc.stderr
        except subprocess.TimeoutExpired as e:
            code, out, err = 124, e.stdout or "", e.stderr or f"timeout after {timeout}s"

        masked = _mask(argv)
        logger.info(
            "cli_run",
            extra={
                "event": "cli_run",
                "argv": masked,
                "exit_code": code,
                "duration_ms": int((time.time() - start) * 1000),
            },
        )
        if code == 0:
            return code, out, err
        last_code, last_out, last_err = code, out, err
        if attempt <= retries:
            backoff = min(5.0, 0.5 * (2 ** (attempt - 1)))
            time.sleep(backoff)
    return last_code, last_out, last_err


# Convenience builders matching the spec
def accounts_create(username: str) -> Tuple[int, str, str]:
    # Some versions of everestctl attempt to open /dev/tty for interactive prompts during create.
    # Allocate a PTY so it can proceed non-interactively.
    return run(["everestctl", "accounts", "create", "-u", username], use_pty=True)


def accounts_set_password(username: str, new_password: Optional[str] = None) -> Tuple[int, str, str]:
    # The CLI may prompt for password; we assume env vars or stdin handling in deployment,
    # but we provide the command wrapper only. Password not passed via argv per security.
    # Send password on stdin if provided.
    stdin_input = None
    if new_password is not None:
        stdin_input = f"{new_password}\n{new_password}\n"
    return run(["everestctl", "accounts", "set-password", "-u", username], stdin_input=stdin_input)


def accounts_list() -> Tuple[int, str, str]:
    return run(["everestctl", "accounts", "list"])


def accounts_delete(username: str) -> Tuple[int, str, str]:
    return run(["everestctl", "accounts", "delete", "-u", username])


def namespaces_add(namespace: str, operators: Dict[str, bool], take_ownership: bool = False) -> Tuple[int, str, str]:
    argv = ["everestctl", "namespaces", "add", namespace]
    for op in ("postgresql", "mongodb", "xtradb-cluster"):
        if op.replace("-", "_") in operators:
            val = operators[op.replace("-", "_")]
            argv.append(f"--operator.{op}={'true' if val else 'false'}")
    if take_ownership:
        argv.append("--take-ownership")
    return run(argv)


def namespaces_update(namespace: str) -> Tuple[int, str, str]:
    return run(["everestctl", "namespaces", "update", namespace])


def namespaces_remove(namespace: str, keep_namespace: bool = False) -> Tuple[int, str, str]:
    argv = ["everestctl", "namespaces", "remove", namespace]
    if keep_namespace:
        argv.append("--keep-namespace")
    return run(argv)


def rbac_validate(policy_file: Optional[str] = None) -> Tuple[int, str, str]:
    argv = ["everestctl", "settings", "rbac", "validate"]
    if policy_file:
        argv += ["--policy-file", policy_file]
    return run(argv)


def rbac_can(policy_file: Optional[str], user: str, resource: str, verb: str, obj: str) -> Tuple[int, str, str]:
    argv = ["everestctl", "settings", "rbac", "can"]
    if policy_file:
        argv += ["--policy-file", policy_file]
    argv += ["--user", user, "--resource", resource, "--verb", verb, "--object", obj]
    return run(argv)


def get_version() -> Tuple[int, str, str]:
    return run(["everestctl", "version"])  # type: ignore[arg-type]


def verify_commands() -> bool:
    if os.environ.get("SKIP_CLI_VERIFY", "false").lower() == "true":
        logger.warning("Skipping everestctl verify due to SKIP_CLI_VERIFY=true")
        return True
    code, out, err = run(["everestctl", "help"])  # type: ignore[arg-type]
    if code != 0:
        logger.error("everestctl help failed: %s", err)
        return False
    # Basic heuristics
    required = ["accounts", "namespaces", "settings"]
    if not all(word in out for word in required):
        logger.error("everestctl missing required commands")
        return False
    return True
