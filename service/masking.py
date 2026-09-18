"""按角色脱敏。

客服、主管、商户、翻译、审计看到同一案件的不同视图。
未知角色一律按最严格策略处理，宁可多遮不可泄露。
"""
from __future__ import annotations

MASK = "***"


def mask_card(value: str | None) -> str | None:
    if not value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return MASK
    return "*" * (len(digits) - 4) + digits[-4:]


def mask_name(value: str | None) -> str | None:
    if not value:
        return value
    return value[0] + "*" * (len(value) - 1) if len(value) > 1 else value + "*"


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return MASK if value else value
    local, domain = value.split("@", 1)
    return (local[0] if local else "*") + "***@" + domain


def mask_phone(value: str | None) -> str | None:
    if not value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 4:
        return MASK
    return "*" * (len(digits) - 4) + digits[-4:]


# 每个角色对敏感字段使用的脱敏函数；None 表示不脱敏（可见原文）。
_FULL = {"card_number": None, "customer_name": None, "customer_email": None, "customer_phone": None}
ROLE_POLICIES: dict[str, dict[str, object]] = {
    "auditor": dict(_FULL),  # 审计导出需要完整链路
    "supervisor": {**_FULL, "card_number": mask_card},
    "agent": {
        "card_number": mask_card,
        "customer_name": mask_name,
        "customer_email": mask_email,
        "customer_phone": mask_phone,
    },
    "merchant": {
        "card_number": mask_card,
        "customer_name": mask_name,
        "customer_email": lambda v: MASK if v else v,
        "customer_phone": lambda v: MASK if v else v,
    },
    "translator": {
        "card_number": lambda v: MASK if v else v,
        "customer_name": mask_name,
        "customer_email": lambda v: MASK if v else v,
        "customer_phone": lambda v: MASK if v else v,
    },
}

# 未知角色：全部敏感字段打码
_STRICT = {
    "card_number": lambda v: MASK if v else v,
    "customer_name": mask_name,
    "customer_email": lambda v: MASK if v else v,
    "customer_phone": lambda v: MASK if v else v,
}

SENSITIVE_FIELDS = ("card_number", "customer_name", "customer_email", "customer_phone")


def mask_view(view: dict, role: str | None) -> dict:
    """对案件视图中的敏感字段按角色脱敏，返回新字典，不改原对象。"""
    policy = ROLE_POLICIES.get(role or "", _STRICT)
    masked = dict(view)
    for field in SENSITIVE_FIELDS:
        fn = policy.get(field)
        if fn is not None and field in masked:
            masked[field] = fn(masked[field])
    return masked
