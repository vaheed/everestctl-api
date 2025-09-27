# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Image includes:
#  - tini (init)
#  - kubectl (pinned/overrideable)
#  - everestctl (latest by default or pinned via build-arg)

# Install tini and helpful tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini curl \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl
# Set KUBECTL_VERSION to a specific tag like v1.30.4 for reproducible builds
ARG KUBECTL_VERSION=stable
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$KUBECTL_VERSION" = "stable" ]; then \
      ver="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"; \
    else \
      ver="$KUBECTL_VERSION"; \
    fi; \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${ver}/bin/linux/${arch}/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client=true --output=yaml >/dev/null 2>&1 || true

# Install everestctl (Percona Everest CLI)
# EVERESTCTL_VERSION accepts values like v0.11.0 or 'latest'
ARG EVERESTCTL_VERSION=latest
ARG EVERESTCTL_REPO=percona/everest
ENV EVERESTCTL_REPO=${EVERESTCTL_REPO}
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) rel_arch="amd64" ;; \
      arm64) rel_arch="arm64" ;; \
      *) echo "unsupported arch: $arch"; exit 1 ;; \
    esac; \
    base="https://github.com/${EVERESTCTL_REPO}/releases"; \
    if [ "$EVERESTCTL_VERSION" = "latest" ]; then \
      url="$base/latest/download/everestctl-linux-$rel_arch"; \
    else \
      url="$base/download/$EVERESTCTL_VERSION/everestctl-linux-$rel_arch"; \
    fi; \
    echo "Downloading everestctl from: $url"; \
    curl -fLSo /tmp/everestctl "$url"; \
    install -m 0555 /tmp/everestctl /usr/local/bin/everestctl; \
    rm -f /tmp/everestctl; \
    /usr/local/bin/everestctl version >/dev/null 2>&1 || true

# everestctl and kubectl are installed at /usr/local/bin

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
    METRICS_ENABLED=true \
    KUBECONFIG=/data/kubeconfig

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini","-g","--"]
CMD ["./entrypoint.sh"]
