FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      tini ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Install kubectl
ARG KUBECTL_VERSION=v1.29.6
RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && /usr/local/bin/kubectl version --client --output=yaml || true

# Install everestctl (override EVERESTCTL_URI at build time if needed)
ARG EVERESTCTL_URI=https://github.com/percona/everest/releases/latest/download/everestctl-linux-amd64
RUN curl -fsSLo /usr/local/bin/everestctl "$EVERESTCTL_URI" \
    && chmod +x /usr/local/bin/everestctl \
    && /usr/local/bin/everestctl help || true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN adduser --disabled-password --gecos '' appuser && \
    mkdir -p /var/lib/everest/policy /var/lib/everest/data && \
    chown -R appuser:appuser /var/lib/everest /app

USER appuser

ENV KUBECONFIG=/data/kubeconfig

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "entrypoint.sh"]
