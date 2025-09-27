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

# Install everestctl (linux/amd64) from percona/everest releases only
# Usage: optionally pass build-arg EVERESTCTL_VERSION (e.g., v0.8.0). If not set, pulls latest.
ARG EVERESTCTL_VERSION=""
RUN set -eux; \
    BASE="https://github.com/percona/everest/releases"; \
    if [ -n "$EVERESTCTL_VERSION" ]; then \
      DL="$BASE/download/${EVERESTCTL_VERSION}"; \
    else \
      DL="$BASE/latest/download"; \
    fi; \
    echo "Downloading everestctl (linux/amd64) from $DL"; \
    mkdir -p /tmp/everest-dl; cd /tmp/everest-dl; \
    set +e; \
    for asset in "everestctl-linux-amd64" "everestctl-amd64" "everestctl"; do \
      url="$DL/$asset"; \
      echo "Trying: $url"; \
      if curl -fsSL -o everestctl.bin "$url"; then \
        echo "Downloaded $asset"; \
        break; \
      fi; \
    done; \
    set -e; \
    if [ ! -s everestctl.bin ]; then \
      echo "Failed to download everestctl from percona/everest releases. Set EVERESTCTL_VERSION to a valid tag."; \
      exit 1; \
    fi; \
    install -m 0755 everestctl.bin /usr/local/bin/everestctl; \
    /usr/local/bin/everestctl --version || true

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Create non-root user; switch if CLIs work under non-root in your env
RUN useradd -u 10001 -m appuser || true
USER 10001

EXPOSE 8080

CMD ["uvicorn", "app.app:app", "--host", "0.0.0.0", "--port", "8080"]
