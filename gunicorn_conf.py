import json
import multiprocessing
import os

bind = os.environ.get("BIND", "0.0.0.0:8080")
workers = int(os.environ.get("WEB_CONCURRENCY", str(multiprocessing.cpu_count() * 2 + 1)))
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()

def access_log_format(sock, addr, request, response, environ, request_time):
    payload = {
        "event": "http_access",
        "remote": addr[0] if addr else None,
        "method": request.method,
        "path": request.path,
        "status": response.status,
        "duration_ms": int(request_time * 1000),
    }
    return json.dumps(payload)

