from __future__ import annotations

from typing import Dict


def build_quota_and_limits_yaml(namespace: str, cpu_cores: int, ram_mb: int, disk_gb: int) -> str:
    """
    Build a multi-document YAML with ResourceQuota and LimitRange.
    Keep simple and deterministic string-based YAML (no PyYAML dependency).
    """
    # Defaults for LimitRange requests/limits per container
    # Choose conservative defaults: requests = 25% of quota, limits = 50% of quota
    # rounded down to at least 1 for CPU and minimal memory.
    req_cpu = max(1, max(1, cpu_cores // 4))
    lim_cpu = max(1, max(1, cpu_cores // 2))
    req_mem = max(256, max(256, (ram_mb // 4)))  # Mi
    lim_mem = max(512, max(512, (ram_mb // 2)))

    quota = f"""
apiVersion: v1
kind: ResourceQuota
metadata:
  name: user-quota
  namespace: {namespace}
spec:
  hard:
    requests.cpu: "{cpu_cores}"
    requests.memory: "{ram_mb}Mi"
    requests.storage: "{disk_gb}Gi"
    limits.cpu: "{cpu_cores}"
    limits.memory: "{ram_mb}Mi"
""".strip()

    limit_range = f"""
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: {namespace}
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: "{req_cpu}"
      memory: "{req_mem}Mi"
    default:
      cpu: "{lim_cpu}"
      memory: "{lim_mem}Mi"
""".strip()

    return f"{quota}\n---\n{limit_range}\n"

