"""按角色做字段级脱敏。

原则：
- 银行原始回执在库中保持原样（只追加、不可改），脱敏只发生在对外视图与通知文本上；
- 脱敏是“按角色”的：同一条数据，主管可见明文，翻译只可见 ***；
- 卡号完整 PAN 永不存储；last4 本身按角色决定是否可见。
"""
from __future__ import annotations

from .models import (
    ROLE_AGENT,
    ROLE_BANK,
    ROLE_MERCHANT,
    ROLE_SUPERVISOR,
    ROLE_TRANSLATOR,
)

SENSITIVE_KEYS = {"customer_name", "name", "customer_email", "email", "card_last4", "last4"}

NAME_KEYS = {"customer_name", "name"}
EMAIL_KEYS = {"customer_email", "email"}
LAST4_KEYS = {"card_last4", "last4"}

MASK = "***"
HIDDEN_EMAIL = "***@masked"


def mask_name(value: str, role: str) -> str:
    if not value:
        return value
    if role in (ROLE_SUPERVISOR, ROLE_AGENT):
        return value
    if role in (ROLE_BANK, ROLE_MERCHANT):
        first = value.strip()[0]
        return f"{first}**"
    return MASK  # TRANSLATOR 等最小权限角色


def mask_email(value: str, role: str) -> str:
    if not value:
        return value
    if role == ROLE_SUPERVISOR:
        return value
    if role == ROLE_AGENT:
        local, _, domain = value.partition("@")
        if not domain:
            return MASK
        head = local[0] if local else "*"
        return f"{head}***@{domain}"
    return HIDDEN_EMAIL


def mask_last4(value: str, role: str) -> str:
    if not value:
        return value
    if role in (ROLE_SUPERVISOR, ROLE_AGENT, ROLE_BANK):
        # 收单行本身持有卡数据；客服需要 last4 核身
        return f"****{value}"
    return "****"


def mask_value(key: str, value, role: str):
    if value is None:
        return None
    if key in NAME_KEYS:
        return mask_name(str(value), role)
    if key in EMAIL_KEYS:
        return mask_email(str(value), role)
    if key in LAST4_KEYS:
        return mask_last4(str(value), role)
    return value


def redact(obj, role: str):
    """递归脱敏 dict / list 中已知的敏感键。"""
    if isinstance(obj, dict):
        return {
            k: mask_value(k, redact(v, role), role) if k in SENSITIVE_KEYS else redact(v, role)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v, role) for v in obj]
    return obj
