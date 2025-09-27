import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple


logger = logging.getLogger(__name__)


class Database:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(sqlite_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self.migrate()

    def migrate(self):
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS limits (
              namespace TEXT PRIMARY KEY,
              max_clusters INTEGER DEFAULT 0,
              allowed_engines TEXT DEFAULT '[]',
              cpu_limit_cores REAL DEFAULT 0,
              memory_limit_bytes INTEGER DEFAULT 0,
              max_db_users INTEGER DEFAULT 0,
              updated_at INTEGER
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_counters (
              namespace TEXT PRIMARY KEY,
              clusters_count INTEGER DEFAULT 0,
              cpu_used REAL DEFAULT 0,
              memory_used INTEGER DEFAULT 0,
              db_users_count INTEGER DEFAULT 0,
              updated_at INTEGER
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER,
              actor TEXT,
              action TEXT,
              namespace TEXT,
              username TEXT,
              request_hash TEXT,
              response_code INTEGER,
              cli_cmd TEXT,
              cli_exit_code INTEGER,
              stdout TEXT,
              stderr TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
              key TEXT PRIMARY KEY,
              created_at INTEGER,
              method TEXT,
              path TEXT,
              body_hash TEXT,
              response_code INTEGER,
              content_type TEXT,
              response_body TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
              name TEXT PRIMARY KEY,
              blueprint TEXT NOT NULL,
              created_at INTEGER,
              updated_at INTEGER
            );
            """
        )
        self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def upsert_limits(self, namespace: str, limits: Dict[str, Any]):
        with self.tx() as cur:
            cur.execute(
                """
                INSERT INTO limits(namespace,max_clusters,allowed_engines,cpu_limit_cores,memory_limit_bytes,max_db_users,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(namespace) DO UPDATE SET
                  max_clusters=excluded.max_clusters,
                  allowed_engines=excluded.allowed_engines,
                  cpu_limit_cores=excluded.cpu_limit_cores,
                  memory_limit_bytes=excluded.memory_limit_bytes,
                  max_db_users=excluded.max_db_users,
                  updated_at=excluded.updated_at
                """,
                (
                    namespace,
                    int(limits.get("max_clusters", 0)),
                    json.dumps(limits.get("allowed_engines", [])),
                    float(limits.get("cpu_limit_cores", 0)),
                    int(limits.get("memory_limit_bytes", 0)),
                    int(limits.get("max_db_users", 0)),
                    int(time.time()),
                ),
            )

    def get_limits(self, namespace: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT namespace,max_clusters,allowed_engines,cpu_limit_cores,memory_limit_bytes,max_db_users FROM limits WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if not row:
            return None
        return {
            "namespace": row[0],
            "max_clusters": row[1],
            "allowed_engines": json.loads(row[2] or "[]"),
            "cpu_limit_cores": row[3],
            "memory_limit_bytes": row[4],
            "max_db_users": row[5],
        }

    def init_usage_if_missing(self, namespace: str):
        with self.tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO usage_counters(namespace,clusters_count,cpu_used,memory_used,db_users_count,updated_at) VALUES(?,?,?,?,?,?)",
                (namespace, 0, 0.0, 0, 0, int(time.time())),
            )

    def get_usage(self, namespace: str) -> Dict[str, Any]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT clusters_count,cpu_used,memory_used,db_users_count FROM usage_counters WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if not row:
            return {"clusters_count": 0, "cpu_used": 0.0, "memory_used": 0, "db_users_count": 0}
        return {
            "clusters_count": row[0],
            "cpu_used": row[1],
            "memory_used": row[2],
            "db_users_count": row[3],
        }

    def apply_cluster_delta(self, namespace: str, op: str, cpu_cores: float, memory_bytes: int):
        delta = 1 if op == "create" else -1
        with self.tx() as cur:
            row = cur.execute(
                "SELECT clusters_count,cpu_used,memory_used FROM usage_counters WHERE namespace=?",
                (namespace,),
            ).fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO usage_counters(namespace,clusters_count,cpu_used,memory_used,db_users_count,updated_at) VALUES(?,?,?,?,?,?)",
                    (namespace, 0, 0.0, 0, 0, int(time.time())),
                )
                row = (0, 0.0, 0)
            clusters, cpu_used, mem_used = row
            clusters += delta
            cpu_used += cpu_cores * delta
            mem_used += int(memory_bytes) * delta
            if clusters < 0 or cpu_used < -1e-9 or mem_used < 0:
                raise ValueError("usage counters underflow")
            cur.execute(
                "UPDATE usage_counters SET clusters_count=?, cpu_used=?, memory_used=?, updated_at=? WHERE namespace=?",
                (clusters, cpu_used, mem_used, int(time.time()), namespace),
            )

    def apply_db_user_delta(self, namespace: str, op: str):
        delta = 1 if op == "create" else -1
        with self.tx() as cur:
            row = cur.execute(
                "SELECT db_users_count FROM usage_counters WHERE namespace=?",
                (namespace,),
            ).fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO usage_counters(namespace,clusters_count,cpu_used,memory_used,db_users_count,updated_at) VALUES(?,?,?,?,?,?)",
                    (namespace, 0, 0.0, 0, 0, int(time.time())),
                )
                count = 0
            else:
                count = row[0]
            count += delta
            if count < 0:
                raise ValueError("db_users_count underflow")
            cur.execute(
                "UPDATE usage_counters SET db_users_count=?, updated_at=? WHERE namespace=?",
                (count, int(time.time()), namespace),
            )

    def write_audit(self, actor: str, action: str, namespace: Optional[str] = None, username: Optional[str] = None, request_hash: Optional[str] = None, response_code: Optional[int] = None, cli_cmd: Optional[str] = None, cli_exit_code: Optional[int] = None, stdout: Optional[str] = None, stderr: Optional[str] = None):
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO audit(ts,actor,action,namespace,username,request_hash,response_code,cli_cmd,cli_exit_code,stdout,stderr) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(time.time()),
                    actor,
                    action,
                    namespace,
                    username,
                    request_hash,
                    response_code,
                    cli_cmd,
                    cli_exit_code,
                    (stdout or "")[:4096],
                    (stderr or "")[:4096],
                ),
            )

    # Idempotency store
    def idempotency_put(self, key: str, content_type: str, status_code: int, body: Any):
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO idempotency_keys(key,created_at,method,path,body_hash,response_code,content_type,response_body) VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    int(time.time()),
                    None,
                    None,
                    None,
                    int(status_code),
                    content_type,
                    json.dumps(body) if content_type == "application/json" else str(body),
                ),
            )

    def idempotency_get(self, key: str) -> Optional[Tuple[str, str, int]]:
        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT response_body, content_type, response_code FROM idempotency_keys WHERE key=?",
            (key,),
        ).fetchone()
        if not row:
            return None
        return (row[0], row[1], int(row[2]))

    def list_tenants(self):
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT l.namespace, l.max_clusters, l.allowed_engines, l.cpu_limit_cores, l.memory_limit_bytes, l.max_db_users, u.clusters_count, u.cpu_used, u.memory_used, u.db_users_count FROM limits l LEFT JOIN usage_counters u ON l.namespace=u.namespace ORDER BY l.namespace"
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "namespace": r[0],
                    "max_clusters": r[1],
                    "allowed_engines": json.loads(r[2] or "[]"),
                    "cpu_limit_cores": r[3],
                    "memory_limit_bytes": r[4],
                    "max_db_users": r[5],
                    "usage": {
                        "clusters_count": r[6] or 0,
                        "cpu_used": r[7] or 0.0,
                        "memory_used": r[8] or 0,
                        "db_users_count": r[9] or 0,
                    },
                }
            )
        return out

    # Templates
    def upsert_template(self, name: str, blueprint: Dict[str, Any]):
        with self.tx() as cur:
            cur.execute(
                """
                INSERT INTO templates(name, blueprint, created_at, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET blueprint=excluded.blueprint, updated_at=excluded.updated_at
                """,
                (name, json.dumps(blueprint), int(time.time()), int(time.time())),
            )

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.cursor()
        row = cur.execute("SELECT blueprint FROM templates WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def delete_template(self, name: str) -> bool:
        with self.tx() as cur:
            cur.execute("DELETE FROM templates WHERE name=?", (name,))
            return cur.rowcount > 0

    def list_templates(self):
        cur = self._conn.cursor()
        rows = cur.execute("SELECT name, blueprint FROM templates ORDER BY name").fetchall()
        return [{"name": r[0], "blueprint": json.loads(r[1] or "{}")} for r in rows]
