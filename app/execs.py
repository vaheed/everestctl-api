import asyncio
import os
import re
import time
from typing import Optional, Sequence, Dict, Any

try:
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover - metrics are optional
    Counter = None  # type: ignore
    Histogram = None  # type: ignore

ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

# Concurrency guard for subprocess calls
_MAX_SUBPROC_CONCURRENCY = int(os.environ.get("MAX_SUBPROC_CONCURRENCY", "16"))
_SUBPROC_SEM = asyncio.Semaphore(_MAX_SUBPROC_CONCURRENCY)

# Prometheus metrics (optional)
if Counter and Histogram:
    CLI_CALLS = Counter(
        "everest_api_cli_calls_total",
        "Total CLI calls",
        labelnames=("tool", "exit_code"),
    )
    CLI_LATENCY = Histogram(
        "everest_api_cli_latency_seconds",
        "CLI call latency in seconds",
        labelnames=("tool",),
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    )
else:  # pragma: no cover - metrics optional
    CLI_CALLS = None  # type: ignore
    CLI_LATENCY = None  # type: ignore


def _strip_ansi(s: str) -> str:
    return ANSI_ESCAPE.sub("", s)


def _truncate(s: str, limit: int = 8000) -> str:
    if len(s) <= limit:
        return s
    head = s[: limit // 2]
    tail = s[-limit // 2 :]
    return f"{head}\n...<truncated {len(s) - limit} bytes>...\n{tail}"


async def run_cli(
    cmd: Sequence[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 60,
    env: Optional[Dict[str, str]] = None,
    retries: int = 0,
    backoff_seconds: float = 0.5,
) -> Dict[str, Any]:
    """Run a CLI safely with timeouts, bounded concurrency, capped output, and optional retries.

    Returns dict with: command, exit_code, stdout, stderr
    """

    # Prepare environment (optionally strict)
    strict_env = os.environ.get("SAFE_SUBPROCESS_ENV", "").lower() in ("1", "true", "yes")
    if strict_env:
        proc_env: Dict[str, str] = {}
        # Whitelist essential vars
        for key in ("PATH", "HOME", "KUBECONFIG", "LANG", "LC_ALL"):
            if key in os.environ:
                proc_env[key] = os.environ[key]
        if env:
            proc_env.update(env)
    else:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

    # Identify tool for metrics label
    tool = cmd[0] if cmd else ""

    attempt = 0
    last_result: Dict[str, Any] = {
        "command": " ".join(cmd),
        "exit_code": 1,
        "stdout": "",
        "stderr": "not executed",
    }

    while True:
        attempt += 1
        start = time.perf_counter()
        try:
            async with _SUBPROC_SEM:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE if input_text is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                )
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(input_text.encode() if input_text is not None else None),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    finally:
                        pass
                    last_result = {
                        "command": " ".join(cmd),
                        "exit_code": 124,
                        "stdout": "",
                        "stderr": f"Command timed out after {timeout}s",
                    }
                    continue  # fall through to retry/metrics logic

            stdout = _truncate(_strip_ansi(stdout_b.decode(errors="replace")))
            stderr = _truncate(_strip_ansi(stderr_b.decode(errors="replace")))
            last_result = {
                "command": " ".join(cmd),
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except FileNotFoundError as e:
            last_result = {
                "command": " ".join(cmd),
                "exit_code": 127,
                "stdout": "",
                "stderr": f"Executable not found: {e}",
            }
        except Exception as e:  # Safety: capture unexpected errors
            last_result = {
                "command": " ".join(cmd),
                "exit_code": 1,
                "stdout": "",
                "stderr": f"Unhandled error: {e}",
            }
        finally:
            if CLI_LATENCY:
                try:
                    CLI_LATENCY.labels(tool=tool or "unknown").observe(max(0.0, time.perf_counter() - start))
                except Exception:
                    pass
            if CLI_CALLS:
                try:
                    CLI_CALLS.labels(tool=tool or "unknown", exit_code=str(last_result.get("exit_code"))).inc()
                except Exception:
                    pass

        # Retry policy: retry on non-zero exit except for 124 (timeout) and 127 (not found)
        if last_result.get("exit_code") in (0, 124, 127) or attempt > retries:
            return last_result
        await asyncio.sleep(backoff_seconds * attempt)


async def run_cmd(
    cmd: Sequence[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 60,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Backwards-compatible wrapper used throughout the app."""
    return await run_cli(cmd, input_text=input_text, timeout=timeout, env=env)

