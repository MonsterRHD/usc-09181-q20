"""时区与截止时间工具。

截止时间一律换算为 UTC 绝对时刻存储（ISO8601，秒级），同时保留
“给人看”的本地标签（如 “2026-09-20 17:00 America/New_York”）。
判断是否超期只比较 UTC 绝对时刻，避免跨境跨日歧义。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# 仅依赖标准库；zoneinfo 在 3.11 标准库中。
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """解析 '...Z' 或带偏移量的 ISO8601；无时区按 UTC 处理。"""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_label(dt_utc: datetime, tz_name: str) -> str:
    try:
        local = dt_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    return local.strftime("%Y-%m-%d %H:%M:%S %Z") + f" [{tz_name}]"


def compute_deadline(
    duration_hours: float,
    *,
    origin: datetime | None = None,
    timezone_name: str = "UTC",
) -> tuple[str, str]:
    """返回 (UTC ISO, 本地可读标签)。duration_hours 可为分数。"""
    start = (origin or now_utc()).astimezone(timezone.utc)
    due = start + timedelta(hours=duration_hours)
    label = f"{local_label(due, timezone_name)} (UTC {to_iso(due)})"
    return to_iso(due), label


def is_past(due_iso: str, *, now: datetime | None = None) -> bool:
    return (now or now_utc()) > parse_iso(due_iso)


def day_bucket_utc(ts_iso: str) -> str:
    """跨日判定使用的 UTC 日桶（也支持回执事件时间）。"""
    return parse_iso(ts_iso).strftime("%Y-%m-%d")


def current_day_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
