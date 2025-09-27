import logging
from typing import Dict, List, Tuple

from db import Database


logger = logging.getLogger(__name__)


def enforce_cluster_create(db: Database, namespace: str, engine: str, cpu_request_cores: float, memory_request_bytes: int) -> Tuple[bool, str]:
    limits = db.get_limits(namespace)
    if not limits:
        return False, "no limits configured for namespace"
    usage = db.get_usage(namespace)

    allowed_engines: List[str] = [str(e) for e in limits.get("allowed_engines", [])]
    if allowed_engines and engine not in allowed_engines:
        return False, f"engine '{engine}' not allowed"

    max_clusters = limits.get("max_clusters", 0)
    if max_clusters and usage.get("clusters_count", 0) + 1 > max_clusters:
        return False, "max clusters exceeded"

    cpu_limit = float(limits.get("cpu_limit_cores", 0.0))
    if cpu_limit and usage.get("cpu_used", 0.0) + float(cpu_request_cores) > cpu_limit + 1e-9:
        return False, "cpu limit exceeded"

    mem_limit = int(limits.get("memory_limit_bytes", 0))
    if mem_limit and usage.get("memory_used", 0) + int(memory_request_bytes) > mem_limit:
        return False, "memory limit exceeded"

    return True, "ok"


def enforce_db_user_create(db: Database, namespace: str) -> Tuple[bool, str]:
    limits = db.get_limits(namespace)
    if not limits:
        return False, "no limits configured for namespace"
    usage = db.get_usage(namespace)
    max_users = limits.get("max_db_users", 0)
    if max_users and usage.get("db_users_count", 0) + 1 > max_users:
        return False, "max db users exceeded"
    return True, "ok"

