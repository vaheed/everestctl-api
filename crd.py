import logging
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional


logger = logging.getLogger(__name__)


CRD_YAML = """
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: tenantresourcepolicies.everest.local
spec:
  group: everest.local
  scope: Namespaced
  names:
    kind: TenantResourcePolicy
    plural: tenantresourcepolicies
    singular: tenantresourcepolicy
    shortNames: [trp]
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          properties:
            spec:
              type: object
              required: [limits, selectors]
              properties:
                limits:
                  type: object
                  properties:
                    cpuCores: { type: number, minimum: 0 }
                    memoryBytes: { type: integer, minimum: 0 }
                    maxClusters: { type: integer, minimum: 0 }
                    maxDbUsers: { type: integer, minimum: 0 }
                selectors:
                  type: object
                  properties:
                    engines:
                      type: array
                      items:
                        type: string
                        enum: [postgresql, mysql, mongodb, xtradb_cluster]
"""


def kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


def ensure_crd_applied() -> bool:
    if not kubectl_available():
        logger.warning("kubectl not available; skipping CRD apply")
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(CRD_YAML)
        path = f.name
    try:
        proc = subprocess.run(["kubectl", "apply", "-f", path], capture_output=True, text=True)
        logger.info("kubectl apply CRD", extra={"event": "crd_apply", "exit_code": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]})
        return proc.returncode == 0
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def tenant_policy_yaml(namespace: str, limits: Dict, engines: List[str]) -> str:
    return (
        "apiVersion: everest.local/v1\n"
        "kind: TenantResourcePolicy\n"
        "metadata:\n"
        f"  name: resource-policy\n  namespace: {namespace}\n"
        "spec:\n"
        "  limits:\n"
        f"    cpuCores: {limits.get('cpu_limit_cores', 0)}\n"
        f"    memoryBytes: {limits.get('memory_limit_bytes', 0)}\n"
        f"    maxClusters: {limits.get('max_clusters', 0)}\n"
        f"    maxDbUsers: {limits.get('max_db_users', 0)}\n"
        "  selectors:\n"
        f"    engines: [{', '.join([repr(e) for e in engines])}]\n"
    )


def upsert_tenant_resource_policy(namespace: str, limits: Dict, engines: List[str]) -> bool:
    if not kubectl_available():
        logger.warning("kubectl not available; cannot apply TenantResourcePolicy")
        return False
    yml = tenant_policy_yaml(namespace, limits, engines)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yml)
        path = f.name
    try:
        proc = subprocess.run(["kubectl", "apply", "-f", path, "-n", namespace], capture_output=True, text=True)
        logger.info(
            "kubectl apply tenant policy",
            extra={"event": "crd_apply_tenant", "namespace": namespace, "exit_code": proc.returncode, "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]},
        )
        return proc.returncode == 0
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

