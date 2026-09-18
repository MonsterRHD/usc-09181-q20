"""SQLite 持久化层与数据库迁移。

使用 SQLite 标准库，零三方依赖。所有写入都在显式事务中提交，
外部请求行带状态字段，程序重启后可按状态恢复追踪。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 1


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    resolution TEXT,
    txn_ref TEXT NOT NULL,          -- 原始交易号（重复申诉判定用）
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    card_last4 TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    txn_time TEXT NOT NULL,
    txn_timezone TEXT NOT NULL,
    merchant_name TEXT NOT NULL,
    acquirer_ref TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    duplicate_of TEXT,
    current_owner TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 客户授权记录（授权窗口、状态、原始快照）
CREATE TABLE IF NOT EXISTS authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    auth_ref TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,          -- TRANSACTION / DISPUTE / REFUND / WITHDRAWAL
    granted INTEGER NOT NULL,
    granted_at TEXT,
    window_start TEXT,
    window_end TEXT,
    snapshot TEXT NOT NULL,          -- 授权时交易快照 JSON
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 银行原始回执：只追加，永不修改、不删除
CREATE TABLE IF NOT EXISTS bank_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    receipt_uid TEXT NOT NULL UNIQUE,   -- 幂等键（收单行原始回执编号）
    receipt_type TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_text TEXT NOT NULL,          -- 原始内容
    currency TEXT,
    amount_minor INTEGER,
    event_time TEXT,          -- 回执上的事件时间（可能跨日）
    received_at TEXT NOT NULL
);

-- 对银行原始回执的更正：客服不能改原始回执，只能追加带原因的更正
CREATE TABLE IF NOT EXISTS receipt_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES bank_receipts(id),
    reason TEXT NOT NULL,
    corrected_payload TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 证据请求
CREATE TABLE IF NOT EXISTS evidence_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    request_ref TEXT NOT NULL UNIQUE,
    target_party TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_summary TEXT,
    completeness TEXT NOT NULL DEFAULT 'INCOMPLETE',          -- COMPLETE / INCOMPLETE
    missing_items TEXT NOT NULL DEFAULT '[]',
    deadline TEXT NOT NULL,          -- ISO8601 UTC，绝对时刻
    deadline_label TEXT NOT NULL,          -- 给人看的时区说明
    requested_at TEXT NOT NULL,
    submitted_at TEXT,
    decided_at TEXT,
    decided_by TEXT
);

-- 部分退款（同一案件可多次提出，各自有状态）
CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    refund_ref TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,          -- PARTIAL / FULL
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    proposed_by TEXT NOT NULL,
    approved_by TEXT,
    linked_receipt_uid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 翻译版本：旧版本不覆盖，标记为 SUPERSEDED
CREATE TABLE IF NOT EXISTS translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    version INTEGER NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated_text TEXT,
    status TEXT NOT NULL,
    translator TEXT,
    requested_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(case_id, version)
);

-- 客户撤回
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    withdrawal_ref TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    reason TEXT,
    requested_at TEXT NOT NULL,
    confirmed_at TEXT,
    processed_by TEXT
);

-- 外部请求（发往收单行/商户/翻译），程序重启后按状态恢复
CREATE TABLE IF NOT EXISTS external_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    request_uid TEXT NOT NULL UNIQUE,
    target_party TEXT NOT NULL,
    request_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,          -- PENDING / DISPATCHED / ACKNOWLEDGED / FAILED
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    idempotency_key TEXT,
    linked_receipt_uid TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 时限
CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    subject_type TEXT NOT NULL,          -- EVIDENCE / CASE / TRANSLATION
    subject_ref TEXT NOT NULL,
    due_at TEXT NOT NULL,          -- ISO8601 UTC 绝对时刻
    due_label TEXT NOT NULL,
    status TEXT NOT NULL,          -- OPEN / MET / BREACHED
    created_at TEXT NOT NULL,
    closed_at TEXT
);

-- 责任链：转派/升级/接手全部留痕
CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    chain_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- TRANSFER / ESCALATION
    from_owner TEXT NOT NULL,
    to_owner TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, chain_seq)
);

-- 通用审计事件
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,          -- JSON
    created_at TEXT NOT NULL
);

-- 通知记录：相同回执只通知一次（receipt_uid 唯一）
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    receipt_uid TEXT NOT NULL,
    channel TEXT NOT NULL,
    target_role TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,          -- 已按目标角色脱敏后的内容
    status TEXT NOT NULL,          -- PENDING / DELIVERED
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(receipt_uid, channel, target_role)
);
"""


class Database:
    """轻量 SQLite 封装：每线程一个连接（check_same_thread=False + 锁）。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("DATABASE_PATH", ":memory:")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        cur = self.execute(sql, params)
        return cur.fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self.execute(sql, params)
        return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_closed", False):
                return
            self._conn.commit()
            self._conn.close()
            self._closed = True


def utcnow_iso() -> str:
    return _utcnow_iso()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("snapshot", "payload", "detail", "missing_items"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (ValueError, TypeError):
                pass
    return d
