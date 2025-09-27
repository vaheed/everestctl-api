import asyncio
import os
import re
from typing import Optional, Sequence, Tuple, Dict, Any

ANSI_ESCAPE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(s: str) -> str:
    return ANSI_ESCAPE.sub("", s)


def _truncate(s: str, limit: int = 8000) -> str:
    if len(s) <= limit:
        return s
    head = s[: limit // 2]
    tail = s[-limit // 2 :]
    return f"{head}\n...<truncated {len(s) - limit} bytes>...\n{tail}"


async def run_cmd(
    cmd: Sequence[str],
    *,
    input_text: Optional[str] = None,
    timeout: int = 60,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Run a shell command asynchronously with a timeout.

    Returns a dict: {
        exit_code, stdout, stderr, command
    }
    """

    # Inherit environment, allow overrides
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    try:
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
            return {
                "command": " ".join(cmd),
                "exit_code": 124,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
            }

        stdout = _truncate(_strip_ansi(stdout_b.decode(errors="replace")))
        stderr = _truncate(_strip_ansi(stderr_b.decode(errors="replace")))
        return {
            "command": " ".join(cmd),
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except FileNotFoundError as e:
        return {
            "command": " ".join(cmd),
            "exit_code": 127,
            "stdout": "",
            "stderr": f"Executable not found: {e}",
        }
    except Exception as e:  # Safety: capture unexpected errors
        return {
            "command": " ".join(cmd),
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Unhandled error: {e}",
        }

