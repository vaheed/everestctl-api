import os
import shutil
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Any


MAX_OUTPUT_CHARS = int(os.environ.get("MAX_OUTPUT_CHARS", "20000"))
DEFAULT_TIMEOUT = int(os.environ.get("SUBPROCESS_TIMEOUT", "60"))


def _strip_ansi(s: str) -> str:
    # Basic ANSI escape removal
    import re

    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", s)


@dataclass
class CmdResult:
    exit_code: int
    stdout: str
    stderr: str
    started_at: float
    finished_at: float


def run_cmd(
    cmd: List[str],
    input_text: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
) -> CmdResult:
    """
    Run a subprocess command with sane defaults and timeouts.
    Returns CmdResult including timestamps. Truncates large outputs.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=input_text.encode() if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={**os.environ, **(env or {})},
        )
        out = proc.stdout.decode(errors="replace")
        err = proc.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired as te:
        out = te.stdout.decode(errors="replace") if te.stdout else ""
        err = (te.stderr.decode(errors="replace") if te.stderr else "") + "\n<timeout>"
        finished = time.time()
        return CmdResult(
            exit_code=124,
            stdout=_strip_and_truncate(out),
            stderr=_strip_and_truncate(err),
            started_at=started,
            finished_at=finished,
        )
    except FileNotFoundError as fnf:
        finished = time.time()
        return CmdResult(
            exit_code=127,
            stdout="",
            stderr=_strip_and_truncate(str(fnf)),
            started_at=started,
            finished_at=finished,
        )

    finished = time.time()
    return CmdResult(
        exit_code=proc.returncode,
        stdout=_strip_and_truncate(out),
        stderr=_strip_and_truncate(err),
        started_at=started,
        finished_at=finished,
    )


def run_cmd_tty(
    cmd: List[str],
    input_text: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    env: Optional[Dict[str, str]] = None,
) -> CmdResult:
    """
    Attempt to run a command with a pseudo-TTY using `script` if available.
    Falls back to run_cmd if `script` is missing.
    """
    if shutil.which("script") is None:
        return run_cmd(cmd, input_text=input_text, timeout=timeout, env=env)

    # Use script -qfc "<cmd>" /dev/null
    # We avoid shell injection by joining with shlex.quote
    quoted = format_command(cmd)
    wrapper = [
        "script",
        "-qfc",
        quoted,
        "/dev/null",
    ]
    return run_cmd(wrapper, input_text=input_text, timeout=timeout, env=env)


def _strip_and_truncate(s: str) -> str:
    s = _strip_ansi(s or "")
    if len(s) > MAX_OUTPUT_CHARS:
        return s[:MAX_OUTPUT_CHARS] + "\n<...truncated...>"
    return s


def format_command(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)
