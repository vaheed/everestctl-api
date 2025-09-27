# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    KUBECONFIG=/root/.kube/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates bash git \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl (linux/amd64)
RUN set -eux; \
    KUBECTL_VERSION=$(curl -L -s https://storage.googleapis.com/kubernetes-release/release/stable.txt); \
    curl -L -o /usr/local/bin/kubectl https://storage.googleapis.com/kubernetes-release/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl; \
    chmod +x /usr/local/bin/kubectl; \
    kubectl version --client=true --short || true

# Install everestctl
# Preferred: official binary placed at /usr/local/bin/everestctl then chmod +x
# Example (adjust URL/version as needed):
# RUN curl -L -o /usr/local/bin/everestctl https://example.com/everestctl/linux/amd64/everestctl \
#     && chmod +x /usr/local/bin/everestctl \
#     && everestctl --version || true
# Fallback: pip install (ensure it provides the CLI)
RUN pip install --no-cache-dir everestctl || true

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Create non-root user; switch if CLIs work under non-root in your env
RUN useradd -u 10001 -m appuser || true
USER 10001

EXPOSE 8080

CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8080"]

