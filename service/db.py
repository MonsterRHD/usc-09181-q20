"""SQLite 持久化层。

每次操作独立开连接（线程安全），写事务统一用 BEGIN IMMEDIATE 串行化，
这样并发转派等场景由数据库保证只有一个事务先提交。
数据库文件落盘，进程重启后未完成的外部请求仍可被查询到。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    customer_name TEXT,
    customer_email TEXT,
    customer_phone TEXT,
    status TEXT NOT NULL,
    resolution TEXT,
    resolution_note TEXT,
    assignee TEXT,
    escalation_level INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    card_number TEXT,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    merchant TEXT,
    merchant_country TEXT,
    occurred_at TEXT
);

CREATE TABLE IF NOT EXISTS authorizations (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    status TEXT NOT NULL,
    channel TEXT,
    reason TEXT,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_requests (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    due_at TEXT,
    due_timezone TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    content TEXT NOT NULL,
    submitted_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    source TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (case_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS receipt_corrections (
    id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    corrected_payload TEXT NOT NULL,
    actor TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    ground TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translations (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    language TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deadlines (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_party TEXT,
    to_party TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    recipient TEXT,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log (case_id, seq);
CREATE INDEX IF NOT EXISTS idx_notifications_case ON notifications (case_id);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence_requests (case_id, status);
"""


class Store:
    """对 SQLite 文件的薄封装：读用 query，写用 write 事务。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def write(self):
        """写事务：BEGIN IMMEDIATE 保证并发写串行，提交前抛错则整体回滚。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str):
    return json.loads(text)
