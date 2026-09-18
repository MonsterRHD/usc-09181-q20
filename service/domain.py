"""领域常量、状态机与错误类型。

所有状态流转都集中在 TRANSITIONS 中声明，service 层在写库前校验，
避免出现"任何状态都能跳到任何状态"的隐式规则。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务错误，携带 HTTP 状态码与稳定错误码，便于 API 层直接映射。"""

    def __init__(self, message: str, status: int = 422, code: str = "DOMAIN_ERROR"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFoundError(DomainError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, 404, "NOT_FOUND")


class ConflictError(DomainError):
    def __init__(self, message: str = "状态冲突"):
        super().__init__(message, 409, "CONFLICT")


class ValidationError(DomainError):
    def __init__(self, message: str = "参数不合法"):
        super().__init__(message, 422, "VALIDATION")


class ForbiddenError(DomainError):
    def __init__(self, message: str = "无权执行该操作"):
        super().__init__(message, 403, "FORBIDDEN")


# ---------------------------------------------------------------- 案件状态
class CaseStatus:
    OPEN = "OPEN"                        # 已建案，待客户授权/分派
    PENDING_EVIDENCE = "PENDING_EVIDENCE"  # 已发出证据请求，等待补证
    UNDER_REVIEW = "UNDER_REVIEW"        # 材料齐全，协调收单行/商户/翻译中
    RESOLVED = "RESOLVED"                # 已出结论
    WITHDRAWN = "WITHDRAWN"              # 客户撤回（终态）
    CLOSED = "CLOSED"                    # 已归档（终态）


CASE_TRANSITIONS = {
    CaseStatus.OPEN: {CaseStatus.PENDING_EVIDENCE, CaseStatus.UNDER_REVIEW,
                      CaseStatus.RESOLVED, CaseStatus.WITHDRAWN},
    CaseStatus.PENDING_EVIDENCE: {CaseStatus.UNDER_REVIEW, CaseStatus.RESOLVED, CaseStatus.WITHDRAWN},
    CaseStatus.UNDER_REVIEW: {CaseStatus.RESOLVED, CaseStatus.PENDING_EVIDENCE, CaseStatus.WITHDRAWN},
    CaseStatus.RESOLVED: {CaseStatus.CLOSED, CaseStatus.UNDER_REVIEW},  # 申诉受理可重开
    CaseStatus.WITHDRAWN: set(),
    CaseStatus.CLOSED: set(),
}

TERMINAL_CASE_STATES = {CaseStatus.WITHDRAWN, CaseStatus.CLOSED}


class Resolution:
    FULL_REFUND = "FULL_REFUND"          # 全额退款
    PARTIAL_REFUND = "PARTIAL_REFUND"    # 部分退款
    MERCHANT_LIABLE = "MERCHANT_LIABLE"  # 商户担责
    REJECTED = "REJECTED"                # 争议不成立


# ---------------------------------------------------------------- 子流程状态
class AuthorizationStatus:
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    REVOKED = "REVOKED"


class EvidenceRequestStatus:
    OPEN = "OPEN"                        # 已发出，未收到任何材料
    PARTIAL = "PARTIALLY_FULFILLED"      # 已收到部分补证材料
    FULFILLED = "FULFILLED"              # 客服确认材料齐全
    EXPIRED = "EXPIRED"                  # 超时未补齐
    CANCELLED = "CANCELLED"              # 案件撤回等原因取消


OPEN_EVIDENCE_STATES = {EvidenceRequestStatus.OPEN, EvidenceRequestStatus.PARTIAL}


class ReceiptStatus:
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"


class CorrectionStatus:
    SUBMITTED = "SUBMITTED"              # 客服提交的更正，原始回执不变
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class RefundStatus:
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    SETTLED = "SETTLED"
    REJECTED = "REJECTED"


REFUND_TRANSITIONS = {
    RefundStatus.PROPOSED: {RefundStatus.APPROVED, RefundStatus.REJECTED},
    RefundStatus.APPROVED: {RefundStatus.SETTLED},
    RefundStatus.SETTLED: set(),
    RefundStatus.REJECTED: set(),
}


class AppealStatus:
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"                # 受理后案件重开
    REJECTED = "REJECTED"                # 含重复申诉


class TranslationStatus:
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"            # 被更新版本取代


class DeadlineStatus:
    PENDING = "PENDING"
    MET = "MET"
    BREACHED = "BREACHED"
    CANCELLED = "CANCELLED"


class AssignmentKind:
    TRANSFER = "TRANSFER"                # 转派
    ESCALATION = "ESCALATION"            # 升级


# ---------------------------------------------------------------- 通知
class NotificationKind:
    RECEIPT_RECEIVED = "RECEIPT_RECEIVED"
    EVIDENCE_REQUESTED = "EVIDENCE_REQUESTED"
    EVIDENCE_SUBMITTED = "EVIDENCE_SUBMITTED"
    REFUND_SETTLED = "REFUND_SETTLED"
    CASE_TRANSFERRED = "CASE_TRANSFERRED"
    CASE_ESCALATED = "CASE_ESCALATED"
    DEADLINE_BREACHED = "DEADLINE_BREACHED"
    CASE_WITHDRAWN = "CASE_WITHDRAWN"
    CASE_RESOLVED = "CASE_RESOLVED"


def ensure_case_transition(current: str, target: str) -> None:
    allowed = CASE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ConflictError(f"案件状态不允许从 {current} 流转到 {target}")
