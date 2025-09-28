import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional


# Context var to store request id per coroutine
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
    return request_id_var.get()


class ContextFilter(logging.Filter):
    """Injects correlation/request id into log records."""

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        rid = get_request_id()
        setattr(record, "request_id", rid)
        return True


class JSONFormatter(logging.Formatter):
    """Simple JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload: Dict[str, Any] = {
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Common extras
        rid = getattr(record, "request_id", None)
        if rid:
            payload["request_id"] = rid
        # Attach any custom extras (flat)
        for key in (
            "event",
            "path",
            "method",
            "status_code",
            "duration_ms",
            "client",
            # Job-related extras
            "job_id",
            "status",
            "step_name",
            "command",
            "exit_code",
            "stdout",
            "stderr",
            "summary",
            "username",
            "namespace",
        ):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root and uvicorn loggers for JSON output with correlation id."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    # Clear existing handlers to avoid duplicate logs
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn loggers
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.addHandler(handler)
        lg.setLevel(level)


async def correlation_middleware(request, call_next):
    """FastAPI middleware: assign request id, log access in JSON."""
    incoming = request.headers.get("X-Request-ID")
    rid = incoming or uuid.uuid4().hex
    token = request_id_var.set(rid)
    start = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
    finally:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        # Create a log record with enriched fields (formatter will inject request_id)
        extra = {
            "event": "http_request",
            "path": str(getattr(request.url, "path", "")),
            "method": request.method,
            "status_code": getattr(response, "status_code", None),
            "duration_ms": duration_ms,
            "client": ",".join([str(v) for v in request.client or ()]) if getattr(request, "client", None) else None,
        }
        logging.getLogger("everestctl_api.access").info("request", extra=extra)
        request_id_var.reset(token)
    # Propagate X-Request-ID
    if response is not None:
        response.headers.setdefault("X-Request-ID", rid)
        return response
    # In case of exception, let upstream handlers manage response; middleware still logs
    raise
