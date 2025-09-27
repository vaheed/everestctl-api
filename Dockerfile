# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install tini and helpful tools
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini && rm -rf /var/lib/apt/lists/*

# Everestctl: provide the binary at runtime via volume or bake your own image
# with the binary placed at /usr/local/bin/everestctl.

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py gunicorn_conf.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Non-root user
RUN addgroup --system app && adduser --system --ingroup app app && \
    mkdir -p /data && chown -R app:app /data /app
USER app

ENV EVERESTCTL_PATH=/usr/local/bin/everestctl \
    RBAC_POLICY_PATH=/data/policy.csv \
    DB_PATH=/data/audit.db \
    METRICS_ENABLED=true

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini","-g","--"]
CMD ["./entrypoint.sh"]
