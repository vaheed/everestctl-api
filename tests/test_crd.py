from crd import tenant_policy_yaml


def test_tenant_policy_yaml():
    y = tenant_policy_yaml(
        "ns-a",
        {
            "max_clusters": 3,
            "allowed_engines": ["postgresql", "mysql"],
            "cpu_limit_cores": 4.0,
            "memory_limit_bytes": 17179869184,
            "max_db_users": 20,
        },
        ["postgresql", "mysql"],
    )
    assert "apiVersion: everest.local/v1" in y
    assert "kind: TenantResourcePolicy" in y
    assert "namespace: ns-a" in y
    assert "cpuCores: 4.0" in y
