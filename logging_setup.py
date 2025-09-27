import json
import logging
import sys
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
        }
        extra_keys = [k for k in record.__dict__.keys() if k not in logging.LogRecord.__dict__]
        for k in extra_keys:
            if k in ("message", "asctime"):
                continue
            v = getattr(record, k)
            try:
                json.dumps(v)
                payload[k] = v
            except Exception:
                payload[k] = str(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger_name: str = "app"):
        super().__init__(app)
        self.logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Response]) -> Response:
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        start = time.time()
        try:
            response = await call_next(request)
            return response
        finally:
            duration = time.time() - start
            self.logger.info(
                "http_request",
                extra={
                    "event": "http_request",
                    "request_id": req_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": getattr(locals().get("response", None), "status_code", None),
                    "duration_ms": int(duration * 1000),
                    "client_ip": request.client.host if request.client else None,
                },
            )

