# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl bash git && \
    rm -rf /var/lib/apt/lists/*

# Install kubectl (configurable version; default stable)
ARG KUBECTL_VERSION=stable
RUN if [ "$KUBECTL_VERSION" = "stable" ]; then \
      KVER=$(curl -sSL https://dl.k8s.io/release/stable.txt); \
    else \
      KVER="$KUBECTL_VERSION"; \
    fi && \
    curl -sSL -o /usr/local/bin/kubectl https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl && \
    chmod +x /usr/local/bin/kubectl

# Install everestctl (configurable version; 'latest' by default)
ARG EVERESTCTL_VERSION=latest
RUN if [ "$EVERESTCTL_VERSION" = "latest" ]; then \
      curl -sSL -o /usr/local/bin/everestctl https://github.com/percona/everest/releases/latest/download/everestctl-linux-amd64; \
    else \
      curl -sSL -o /usr/local/bin/everestctl https://github.com/percona/everest/releases/download/${EVERESTCTL_VERSION}/everestctl-linux-amd64; \
    fi && \
    chmod +x /usr/local/bin/everestctl || true
# Fallback (uncomment if needed):
# RUN pip install --no-cache-dir everestctl

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV KUBECONFIG=/root/.kube/config \
    ADMIN_API_KEY=changeme \
    PORT=8080

# Create non-root user
RUN useradd -u 10001 -m appuser || true
USER 10001

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
