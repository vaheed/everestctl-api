import os
from app.k8s import build_quota_limitrange_yaml


def test_quota_includes_count_keys(monkeypatch):
    monkeypatch.setenv("EVEREST_DB_COUNT_RESOURCES", "foo.example.com,bar.example.com")
    yaml_text = build_quota_limitrange_yaml("ns1", {"cpu_cores": 1, "ram_mb": 512, "disk_gb": 1, "max_databases": 2})
    assert "count/foo.example.com: \"2\"" in yaml_text
    assert "count/bar.example.com: \"2\"" in yaml_text

