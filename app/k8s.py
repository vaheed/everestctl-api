import os
from typing import Any, Dict, List


def build_quota_limitrange_yaml(namespace: str, resources: Dict[str, Any]) -> str:
    """
    Build a combined YAML for ResourceQuota and LimitRange.

    resources expects keys: cpu_cores, ram_mb, disk_gb
    """
    cpu = int(resources.get("cpu_cores", 2))
    ram_mb = int(resources.get("ram_mb", 2048))
    disk_gb = int(resources.get("disk_gb", 20))
    max_dbs = resources.get("max_databases")

    # Parse optional DB count resources to enforce via ResourceQuota count/<resource>
    # Comma-separated list, e.g.:
    #   perconaservermongodbs.psmdb.percona.com,perconapgclusters.pgv2.percona.com,perconaxtradbclusters.pxc.percona.com
    count_resources_env = os.environ.get("EVEREST_DB_COUNT_RESOURCES", "").strip()
    count_resources: List[str] = [r.strip() for r in count_resources_env.split(",") if r.strip()]

    # Simple defaults for LimitRange (per container)
    limitrange = {
        "defaultRequest": {"cpu": "1", "memory": "512Mi"},
        "default": {"cpu": "1", "memory": "1024Mi"},
    }

    # Base quota with CPU/memory/storage
    quota_yaml = f"""
apiVersion: v1
kind: ResourceQuota
metadata:
  name: user-quota
  namespace: {namespace}
spec:
  hard:
    requests.cpu: "{cpu}"
    requests.memory: "{ram_mb}Mi"
    requests.storage: "{disk_gb}Gi"
    limits.cpu: "{cpu}"
    limits.memory: "{ram_mb}Mi"
""".strip()

    # Append count quotas if configured
    if max_dbs is not None and count_resources:
        try:
            max_dbs_int = int(max_dbs)
        except Exception:
            max_dbs_int = None
        if max_dbs_int is not None and max_dbs_int >= 0:
            count_lines = []
            for res in count_resources:
                count_lines.append(f"    count/{res}: \"{max_dbs_int}\"")
            quota_yaml = quota_yaml + "\n" + "\n".join(count_lines)

    limitrange_yaml = f"""
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: {namespace}
spec:
  limits:
  - type: Container
    defaultRequest:
      cpu: "{limitrange['defaultRequest']['cpu']}"
      memory: "{limitrange['defaultRequest']['memory']}"
    default:
      cpu: "{limitrange['default']['cpu']}"
      memory: "{limitrange['default']['memory']}"
""".strip()

    return quota_yaml + "\n---\n" + limitrange_yaml + "\n"


def build_scale_statefulsets_cmd(namespace: str) -> List[str]:
    """
    Return a kubectl command to scale down all StatefulSets in a namespace to 0.
    """
    return ["kubectl", "scale", "statefulset", "--all", "-n", namespace, "--replicas=0"]
