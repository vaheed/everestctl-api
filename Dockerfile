# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    KUBECONFIG=/root/.kube/config

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl bash gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl (stable Linux amd64)
# Reference: https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/
RUN set -eux; \
    KVER=$(curl -L -s https://dl.k8s.io/release/stable.txt); \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client --output=yaml || true

# Install everestctl
# Note: Update the URL below to the correct everestctl binary release for Linux amd64.
# If everestctl is a Python package, uncomment the pip install fallback.
ENV EVERESTCTL_URL="https://example.com/everestctl/releases/latest/linux-amd64/everestctl"
RUN set -eux; \
    if curl -fsSLo /usr/local/bin/everestctl "$EVERESTCTL_URL"; then \
      chmod +x /usr/local/bin/everestctl; \
      /usr/local/bin/everestctl --help || true; \
    else \
      echo "Warning: direct download failed; trying pip install everestctl"; \
      pip install --no-cache-dir everestctl || true; \
    fi

# Python deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app ./app

EXPOSE 8080

# Start the server; PORT is read at runtime
CMD ["sh", "-c", "uvicorn app.app:app --host 0.0.0.0 --port ${PORT:-8080}"]

