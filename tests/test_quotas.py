from db import Database
from quotas import enforce_cluster_create, enforce_db_user_create


def test_enforce_limits(tmp_path):
    db = Database(str(tmp_path / "db.sqlite"))
    ns = "ns-alice"
    db.upsert_limits(
        ns,
        {
            "max_clusters": 1,
            "allowed_engines": ["postgresql"],
            "cpu_limit_cores": 2.0,
            "memory_limit_bytes": 1024 * 1024 * 1024,
            "max_db_users": 1,
        },
    )
    ok, reason = enforce_cluster_create(db, ns, "postgresql", 1.0, 512 * 1024 * 1024)
    assert ok
    db.apply_cluster_delta(ns, "create", 1.0, 512 * 1024 * 1024)
    ok, reason = enforce_cluster_create(db, ns, "postgresql", 1.0, 512 * 1024 * 1024)
    assert not ok and "max clusters" in reason

    ok, _ = enforce_db_user_create(db, ns)
    assert ok
    db.apply_db_user_delta(ns, "create")
    ok, _ = enforce_db_user_create(db, ns)
    assert not ok

