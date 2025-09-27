import multiprocessing
import os

bind = os.environ.get("BIND", "0.0.0.0:8080")
workers = int(os.environ.get("WEB_CONCURRENCY", str(multiprocessing.cpu_count() * 2 + 1)))
worker_class = "uvicorn.workers.UvicornWorker"
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()

# Gunicorn expects a format string, not a callable, for access logs.
# JSON-like format using documented placeholders.
access_log_format = (
    '{"event":"http_access","remote":"%(h)s","time":"%(t)s",'
    '"request":"%(r)s","status":%(s)s,"length":"%(b)s","referer":"%(f)s",'
    '"agent":"%(a)s","duration":"%(L)s"}'
)
