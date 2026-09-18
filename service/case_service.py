"""案件服务：把交易、授权、证据、回执、退款、时限组织成一条可审计的案件流程。

每个公开方法对应一次业务操作：
- 写操作都在单个事务内完成，并同步写审计日志与通知（通知按 dedup_key 去重）；
- 外部回执按 (case_id, idempotency_key) 幂等，重复到达不产生新通知；
- 银行原始回执只读，更正以独立记录挂在回执上；
- 转派/升级走乐观锁（expected_version），并发时只有一个生效，责任链完整。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .db import Store, dumps, loads
from .domain import (
    AppealStatus,
    AssignmentKind,
    AuthorizationStatus,
    CaseStatus,
    CorrectionStatus,
    DeadlineStatus,
    EvidenceRequestStatus,
    NotificationKind,
    OPEN_EVIDENCE_STATES,
    ReceiptStatus,
    REFUND_TRANSITIONS,
    RefundStatus,
    Resolution,
    TERMINAL_CASE_STATES,
    TranslationStatus,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
    ensure_case_transition,
)
from .masking import mask_view


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(dt_text: str) -> datetime:
    dt = datetime.fromisoformat(dt_text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class CaseService:
    def __init__(self, store: Store, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ 基础工具
    def _now(self) -> datetime:
        return self.clock()

    def _audit(self, conn, case_id: str, actor: str, action: str, detail: dict | None = None):
        conn.execute(
            "INSERT INTO audit_log (case_id, actor, action, detail, created_at) VALUES (?,?,?,?,?)",
            (case_id, actor, action, dumps(detail or {}), _iso(self._now())),
        )

    def _notify(self, conn, case_id: str, dedup_key: str, kind: str, recipient: str, message: str) -> bool:
        """按 dedup_key 去重发通知；返回是否真正产生了新通知。"""
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications (id, case_id, dedup_key, kind, recipient, message, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (_id("ntf"), case_id, dedup_key, kind, recipient, message, _iso(self._now())),
        )
        return cur.rowcount == 1

    def _get_case_row(self, conn, case_id: str) -> dict:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        return dict(row)

    def get_case(self, case_id: str) -> dict:
        row = self.store.query_one("SELECT * FROM cases WHERE id = ?", (case_id,))
        if row is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        return row

    # ------------------------------------------------------------ 建案与授权
    def create_case(self, transaction: dict, customer: dict, tz: str = "UTC",
                    actor: str = "system", assignee: str | None = None) -> dict:
        if not transaction.get("amount_cents") or transaction["amount_cents"] <= 0:
            raise ValidationError("交易金额必须为正整数（分）")
        if not transaction.get("currency"):
            raise ValidationError("缺少交易币种")
        try:
            ZoneInfo(tz)
        except Exception as exc:
            raise ValidationError(f"未知时区: {tz}") from exc

        now = _iso(self._now())
        case_id, txn_id = _id("case"), _id("txn")
        with self.store.write() as conn:
            conn.execute(
                "INSERT INTO transactions (id, card_number, amount_cents, currency, merchant, merchant_country, occurred_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    txn_id,
                    transaction.get("card_number"),
                    transaction["amount_cents"],
                    transaction["currency"],
                    transaction.get("merchant"),
                    transaction.get("merchant_country"),
                    transaction.get("occurred_at", now),
                ),
            )
            conn.execute(
                "INSERT INTO cases (id, transaction_id, customer_name, customer_email, customer_phone,"
                " status, assignee, timezone, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id, txn_id,
                    customer.get("name"), customer.get("email"), customer.get("phone"),
                    CaseStatus.OPEN, assignee, tz, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO authorizations (id, case_id, status, channel, actor, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (_id("auth"), case_id, AuthorizationStatus.PENDING, None, actor, now),
            )
            self._audit(conn, case_id, actor, "CASE_CREATED",
                        {"transaction_id": txn_id, "amount_cents": transaction["amount_cents"],
                         "currency": transaction["currency"]})
        return self.get_case_view(case_id, role="auditor")

    def set_authorization(self, case_id: str, granted: bool, channel: str,
                          actor: str, reason: str | None = None) -> dict:
        status = AuthorizationStatus.GRANTED if granted else AuthorizationStatus.REVOKED
        now = _iso(self._now())
        with self.store.write() as conn:
            self._get_case_row(conn, case_id)
            conn.execute(
                "INSERT INTO authorizations (id, case_id, status, channel, reason, actor, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (_id("auth"), case_id, status, channel, reason, actor, now),
            )
            self._audit(conn, case_id, actor, "AUTHORIZATION_GRANTED" if granted else "AUTHORIZATION_REVOKED",
                        {"channel": channel, "reason": reason})
        return {"case_id": case_id, "authorization": status}

    # ------------------------------------------------------------ 证据请求与补证
    def create_evidence_request(self, case_id: str, description: str, actor: str,
                                due_local: str | None = None, due_timezone: str | None = None) -> dict:
        """发出证据请求；可同时登记带时区的截止时间（按当地日期时间换算为 UTC 存储）。"""
        if not description:
            raise ValidationError("证据请求缺少描述")
        now_dt = self._now()
        now = _iso(now_dt)
        req_id = _id("evr")
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            if case["status"] in TERMINAL_CASE_STATES:
                raise ConflictError(f"案件已终结（{case['status']}），不能再发证据请求")
            due_at = None
            tz_name = due_timezone or case["timezone"]
            if due_local:
                local_dt = datetime.fromisoformat(due_local)
                if local_dt.tzinfo is None:
                    local_dt = local_dt.replace(tzinfo=ZoneInfo(tz_name))
                due_at = _iso(local_dt)
                conn.execute(
                    "INSERT INTO deadlines (id, case_id, kind, due_at, timezone, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (_id("ddl"), case_id, "EVIDENCE", due_at, tz_name, DeadlineStatus.PENDING, now, now),
                )
            conn.execute(
                "INSERT INTO evidence_requests (id, case_id, description, status, due_at, due_timezone, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (req_id, case_id, description, EvidenceRequestStatus.OPEN, due_at, tz_name if due_at else None, now, now),
            )
            if case["status"] in (CaseStatus.OPEN, CaseStatus.UNDER_REVIEW):
                ensure_case_transition(case["status"], CaseStatus.PENDING_EVIDENCE)
                conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?",
                             (CaseStatus.PENDING_EVIDENCE, now, case_id))
            self._notify(conn, case_id, f"evidence-request:{req_id}", NotificationKind.EVIDENCE_REQUESTED,
                         case["assignee"] or "unassigned", f"证据请求 {req_id}: {description}")
            self._audit(conn, case_id, actor, "EVIDENCE_REQUESTED",
                        {"request_id": req_id, "description": description, "due_at": due_at, "timezone": tz_name})
        return self.get_evidence_request(req_id)

    def submit_evidence(self, request_id: str, content: str, actor: str) -> dict:
        """补证：向证据请求提交材料。请求先进入部分完成，确认齐全后再 fulfill。"""
        if not content:
            raise ValidationError("补证材料内容不能为空")
        now = _iso(self._now())
        with self.store.write() as conn:
            req = conn.execute("SELECT * FROM evidence_requests WHERE id = ?", (request_id,)).fetchone()
            if req is None:
                raise NotFoundError(f"证据请求不存在: {request_id}")
            req = dict(req)
            if req["status"] not in OPEN_EVIDENCE_STATES:
                raise ConflictError(f"证据请求当前状态 {req['status']} 不可补证")
            item_id = _id("evi")
            conn.execute(
                "INSERT INTO evidence_items (id, request_id, case_id, content, submitted_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (item_id, request_id, req["case_id"], content, actor, now),
            )
            conn.execute("UPDATE evidence_requests SET status=?, updated_at=? WHERE id=?",
                         (EvidenceRequestStatus.PARTIAL, now, request_id))
            self._notify(conn, req["case_id"], f"evidence-item:{item_id}", NotificationKind.EVIDENCE_SUBMITTED,
                         actor, f"补证材料 {item_id} 已提交")
            self._audit(conn, req["case_id"], actor, "EVIDENCE_SUBMITTED",
                        {"request_id": request_id, "item_id": item_id})
        return {"request_id": request_id, "item_id": item_id, "status": EvidenceRequestStatus.PARTIAL}

    def fulfill_evidence_request(self, request_id: str, actor: str) -> dict:
        """客服确认材料齐全；若案件无其他未完成的证据请求，则回到 UNDER_REVIEW。"""
        now = _iso(self._now())
        with self.store.write() as conn:
            req = conn.execute("SELECT * FROM evidence_requests WHERE id = ?", (request_id,)).fetchone()
            if req is None:
                raise NotFoundError(f"证据请求不存在: {request_id}")
            req = dict(req)
            if req["status"] not in OPEN_EVIDENCE_STATES:
                raise ConflictError(f"证据请求当前状态 {req['status']} 不可确认完成")
            items = conn.execute("SELECT COUNT(*) AS n FROM evidence_items WHERE request_id = ?",
                                 (request_id,)).fetchone()["n"]
            if items == 0:
                raise ConflictError("尚未收到任何补证材料，不能确认完成")
            conn.execute("UPDATE evidence_requests SET status=?, updated_at=? WHERE id=?",
                         (EvidenceRequestStatus.FULFILLED, now, request_id))
            case_id = req["case_id"]
            self._mark_deadline_met(conn, case_id, "EVIDENCE")
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_requests WHERE case_id = ? AND status IN (?, ?)",
                (case_id, EvidenceRequestStatus.OPEN, EvidenceRequestStatus.PARTIAL)).fetchone()["n"]
            case = self._get_case_row(conn, case_id)
            if remaining == 0 and case["status"] == CaseStatus.PENDING_EVIDENCE:
                ensure_case_transition(case["status"], CaseStatus.UNDER_REVIEW)
                conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?",
                             (CaseStatus.UNDER_REVIEW, now, case_id))
            self._audit(conn, case_id, actor, "EVIDENCE_FULFILLED", {"request_id": request_id})
        return self.get_evidence_request(request_id)

    def get_evidence_request(self, request_id: str) -> dict:
        req = self.store.query_one("SELECT * FROM evidence_requests WHERE id = ?", (request_id,))
        if req is None:
            raise NotFoundError(f"证据请求不存在: {request_id}")
        req["items"] = self.store.query(
            "SELECT * FROM evidence_items WHERE request_id = ? ORDER BY created_at", (request_id,))
        return req

    # ------------------------------------------------------------ 外部回执（幂等、只读、可更正）
    def receive_receipt(self, case_id: str, source: str, idempotency_key: str, payload: dict,
                        actor: str = "external-gateway") -> dict:
        """接收收单行/商户回执。

        相同 (case_id, idempotency_key) 重复到达时直接返回首次记录，
        不重复写审计、不重复发通知。回执落库后不可修改。
        """
        if not idempotency_key:
            raise ValidationError("回执缺少幂等键")
        if not source:
            raise ValidationError("回执缺少来源")
        now_dt = self._now()
        now = _iso(now_dt)
        receipt_id = None
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            existing = conn.execute(
                "SELECT * FROM receipts WHERE case_id = ? AND idempotency_key = ?",
                (case_id, idempotency_key)).fetchone()
            if existing is not None:
                receipt = dict(existing)
                receipt["payload"] = loads(receipt["payload"])
                receipt["duplicate"] = True
                return receipt

            local_date = now_dt.astimezone(ZoneInfo(case["timezone"])).date().isoformat()
            receipt_id = _id("rcpt")
            conn.execute(
                "INSERT INTO receipts (id, case_id, source, idempotency_key, payload, received_at, local_date, status)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (receipt_id, case_id, source, idempotency_key, dumps(payload), now, local_date,
                 ReceiptStatus.RECEIVED),
            )
            self._notify(conn, case_id, f"receipt:{case_id}:{idempotency_key}",
                         NotificationKind.RECEIPT_RECEIVED, case["assignee"] or "unassigned",
                         f"收到{source}回执 {receipt_id}")
            self._audit(conn, case_id, actor, "RECEIPT_RECEIVED",
                        {"receipt_id": receipt_id, "source": source,
                         "idempotency_key": idempotency_key, "local_date": local_date})
        receipt = self.get_receipt(receipt_id)
        receipt["duplicate"] = False
        return receipt

    def get_receipt(self, receipt_id: str) -> dict:
        row = self.store.query_one("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
        if row is None:
            raise NotFoundError(f"回执不存在: {receipt_id}")
        row["payload"] = loads(row["payload"])
        row["corrections"] = self.store.query(
            "SELECT * FROM receipt_corrections WHERE receipt_id = ? ORDER BY created_at", (receipt_id,))
        for c in row["corrections"]:
            c["corrected_payload"] = loads(c["corrected_payload"])
        return row

    def correct_receipt(self, receipt_id: str, corrected_payload: dict, reason: str, actor: str) -> dict:
        """对回执提交带原因的更正；原始回执保持原样，更正单独立账。"""
        if not reason:
            raise ValidationError("更正必须说明原因")
        if not corrected_payload:
            raise ValidationError("更正内容不能为空")
        now = _iso(self._now())
        correction_id = None
        with self.store.write() as conn:
            rcpt = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
            if rcpt is None:
                raise NotFoundError(f"回执不存在: {receipt_id}")
            correction_id = _id("cor")
            conn.execute(
                "INSERT INTO receipt_corrections (id, receipt_id, case_id, reason, corrected_payload, actor, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (correction_id, receipt_id, rcpt["case_id"], reason, dumps(corrected_payload), actor,
                 CorrectionStatus.SUBMITTED, now),
            )
            self._audit(conn, rcpt["case_id"], actor, "RECEIPT_CORRECTED",
                        {"receipt_id": receipt_id, "correction_id": correction_id, "reason": reason})
        row = self.store.query_one("SELECT * FROM receipt_corrections WHERE id = ?", (correction_id,))
        row["corrected_payload"] = loads(row["corrected_payload"])
        return row

    # ------------------------------------------------------------ 部分退款
    def propose_refund(self, case_id: str, amount_cents: int, reason: str, actor: str) -> dict:
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValidationError("退款金额必须为正整数（分）")
        now = _iso(self._now())
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            if case["status"] in TERMINAL_CASE_STATES:
                raise ConflictError(f"案件已终结（{case['status']}），不能发起退款")
            txn = conn.execute("SELECT * FROM transactions WHERE id = ?",
                               (case["transaction_id"],)).fetchone()
            committed = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refunds"
                " WHERE case_id = ? AND status != ?",
                (case_id, RefundStatus.REJECTED)).fetchone()["s"]
            if committed + amount_cents > txn["amount_cents"]:
                raise ValidationError(
                    f"退款累计 {committed + amount_cents} 分超出交易金额 {txn['amount_cents']} 分")
            refund_id = _id("rfd")
            conn.execute(
                "INSERT INTO refunds (id, case_id, amount_cents, reason, status, actor, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (refund_id, case_id, amount_cents, reason, RefundStatus.PROPOSED, actor, now, now),
            )
            self._audit(conn, case_id, actor, "REFUND_PROPOSED",
                        {"refund_id": refund_id, "amount_cents": amount_cents, "reason": reason})
        return self.get_refund(refund_id)

    def _transition_refund(self, refund_id: str, target: str, actor: str) -> dict:
        now = _iso(self._now())
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM refunds WHERE id = ?", (refund_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"退款单不存在: {refund_id}")
            row = dict(row)
            if target not in REFUND_TRANSITIONS.get(row["status"], set()):
                raise ConflictError(f"退款单不允许从 {row['status']} 流转到 {target}")
            conn.execute("UPDATE refunds SET status=?, updated_at=? WHERE id=?", (target, now, refund_id))
            action = {RefundStatus.APPROVED: "REFUND_APPROVED",
                      RefundStatus.SETTLED: "REFUND_SETTLED",
                      RefundStatus.REJECTED: "REFUND_REJECTED"}[target]
            self._audit(conn, row["case_id"], actor, action,
                        {"refund_id": refund_id, "amount_cents": row["amount_cents"]})
            if target == RefundStatus.SETTLED:
                self._notify(conn, row["case_id"], f"refund-settled:{refund_id}",
                             NotificationKind.REFUND_SETTLED, "customer",
                             f"退款 {row['amount_cents']} 分已到账")
        return self.get_refund(refund_id)

    def approve_refund(self, refund_id: str, actor: str) -> dict:
        return self._transition_refund(refund_id, RefundStatus.APPROVED, actor)

    def settle_refund(self, refund_id: str, actor: str) -> dict:
        return self._transition_refund(refund_id, RefundStatus.SETTLED, actor)

    def reject_refund(self, refund_id: str, actor: str) -> dict:
        return self._transition_refund(refund_id, RefundStatus.REJECTED, actor)

    def get_refund(self, refund_id: str) -> dict:
        row = self.store.query_one("SELECT * FROM refunds WHERE id = ?", (refund_id,))
        if row is None:
            raise NotFoundError(f"退款单不存在: {refund_id}")
        return row

    # ------------------------------------------------------------ 重复申诉
    def submit_appeal(self, case_id: str, ground: str, actor: str) -> dict:
        """对已出结论的案件发起申诉；同一理由已有在处理申诉时判重拒绝。"""
        if not ground:
            raise ValidationError("申诉理由不能为空")
        now = _iso(self._now())
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            if case["status"] != CaseStatus.RESOLVED:
                raise ConflictError("只有已出结论的案件才能申诉")
            dup = conn.execute(
                "SELECT id FROM appeals WHERE case_id = ? AND ground = ? AND status = ?",
                (case_id, ground, AppealStatus.SUBMITTED)).fetchone()
            if dup is not None:
                raise ConflictError(f"相同理由的申诉 {dup['id']} 正在处理，请勿重复提交")
            appeal_id = _id("apl")
            conn.execute(
                "INSERT INTO appeals (id, case_id, ground, status, actor, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (appeal_id, case_id, ground, AppealStatus.SUBMITTED, actor, now, now),
            )
            self._audit(conn, case_id, actor, "APPEAL_SUBMITTED", {"appeal_id": appeal_id, "ground": ground})
        return self.get_appeal(appeal_id)

    def accept_appeal(self, appeal_id: str, actor: str) -> dict:
        """受理申诉：案件从 RESOLVED 重开为 UNDER_REVIEW，原结论保留在审计链中。"""
        now = _iso(self._now())
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"申诉不存在: {appeal_id}")
            row = dict(row)
            if row["status"] != AppealStatus.SUBMITTED:
                raise ConflictError(f"申诉当前状态 {row['status']} 不可受理")
            conn.execute("UPDATE appeals SET status=?, updated_at=? WHERE id=?",
                         (AppealStatus.ACCEPTED, now, appeal_id))
            case = self._get_case_row(conn, row["case_id"])
            ensure_case_transition(case["status"], CaseStatus.UNDER_REVIEW)
            conn.execute("UPDATE cases SET status=?, resolution=NULL, resolution_note=NULL, updated_at=? WHERE id=?",
                         (CaseStatus.UNDER_REVIEW, now, row["case_id"]))
            self._audit(conn, row["case_id"], actor, "APPEAL_ACCEPTED",
                        {"appeal_id": appeal_id, "previous_resolution": case["resolution"]})
        return self.get_appeal(appeal_id)

    def reject_appeal(self, appeal_id: str, actor: str, reason: str | None = None) -> dict:
        now = _iso(self._now())
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"申诉不存在: {appeal_id}")
            row = dict(row)
            if row["status"] != AppealStatus.SUBMITTED:
                raise ConflictError(f"申诉当前状态 {row['status']} 不可驳回")
            conn.execute("UPDATE appeals SET status=?, updated_at=? WHERE id=?",
                         (AppealStatus.REJECTED, now, appeal_id))
            self._audit(conn, row["case_id"], actor, "APPEAL_REJECTED",
                        {"appeal_id": appeal_id, "reason": reason})
        return self.get_appeal(appeal_id)

    def get_appeal(self, appeal_id: str) -> dict:
        row = self.store.query_one("SELECT * FROM appeals WHERE id = ?", (appeal_id,))
        if row is None:
            raise NotFoundError(f"申诉不存在: {appeal_id}")
        return row

    # ------------------------------------------------------------ 翻译版本
    def add_translation(self, case_id: str, document_ref: str, language: str,
                        content: str, actor: str) -> dict:
        """登记翻译版本；同一文档同一语言的新版本会把旧版本置为 SUPERSEDED。"""
        if not all([document_ref, language, content]):
            raise ValidationError("翻译缺少文档、语言或内容")
        now = _iso(self._now())
        with self.store.write() as conn:
            self._get_case_row(conn, case_id)
            row = conn.execute(
                "SELECT MAX(version) AS v FROM translations WHERE case_id=? AND document_ref=? AND language=?",
                (case_id, document_ref, language)).fetchone()
            version = (row["v"] or 0) + 1
            conn.execute(
                "UPDATE translations SET status=? WHERE case_id=? AND document_ref=? AND language=? AND status!=?",
                (TranslationStatus.SUPERSEDED, case_id, document_ref, language, TranslationStatus.SUPERSEDED))
            translation_id = _id("trl")
            conn.execute(
                "INSERT INTO translations (id, case_id, document_ref, language, version, content, status, actor, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (translation_id, case_id, document_ref, language, version, content,
                 TranslationStatus.DRAFT, actor, now),
            )
            self._audit(conn, case_id, actor, "TRANSLATION_ADDED",
                        {"translation_id": translation_id, "document_ref": document_ref,
                         "language": language, "version": version})
        return self.get_translation(translation_id)

    def approve_translation(self, translation_id: str, actor: str) -> dict:
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM translations WHERE id = ?", (translation_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"翻译不存在: {translation_id}")
            row = dict(row)
            if row["status"] != TranslationStatus.DRAFT:
                raise ConflictError(f"翻译当前状态 {row['status']} 不可审核通过")
            conn.execute("UPDATE translations SET status=? WHERE id=?",
                         (TranslationStatus.APPROVED, translation_id))
            self._audit(conn, row["case_id"], actor, "TRANSLATION_APPROVED",
                        {"translation_id": translation_id})
        return self.get_translation(translation_id)

    def get_translation(self, translation_id: str) -> dict:
        row = self.store.query_one("SELECT * FROM translations WHERE id = ?", (translation_id,))
        if row is None:
            raise NotFoundError(f"翻译不存在: {translation_id}")
        return row

    # ------------------------------------------------------------ 客户撤回
    def withdraw_case(self, case_id: str, reason: str, actor: str) -> dict:
        """客户撤回：案件进入终态，未完成的证据请求与时限一并取消。"""
        if not reason:
            raise ValidationError("撤回必须说明原因")
        now = _iso(self._now())
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            ensure_case_transition(case["status"], CaseStatus.WITHDRAWN)
            conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?",
                         (CaseStatus.WITHDRAWN, now, case_id))
            conn.execute("UPDATE evidence_requests SET status=?, updated_at=? WHERE case_id=? AND status IN (?,?)",
                         (EvidenceRequestStatus.CANCELLED, now, case_id,
                          EvidenceRequestStatus.OPEN, EvidenceRequestStatus.PARTIAL))
            conn.execute("UPDATE deadlines SET status=?, updated_at=? WHERE case_id=? AND status=?",
                         (DeadlineStatus.CANCELLED, now, case_id, DeadlineStatus.PENDING))
            conn.execute("INSERT INTO withdrawals (id, case_id, reason, actor, created_at) VALUES (?,?,?,?,?)",
                         (_id("wdw"), case_id, reason, actor, now))
            self._notify(conn, case_id, f"withdrawn:{case_id}", NotificationKind.CASE_WITHDRAWN,
                         case["assignee"] or "unassigned", f"客户撤回争议：{reason}")
            self._audit(conn, case_id, actor, "CASE_WITHDRAWN", {"reason": reason})
        return self.get_case(case_id)

    # ------------------------------------------------------------ 转派与升级（责任链）
    def transfer_case(self, case_id: str, to_assignee: str, reason: str, actor: str,
                      expected_version: int) -> dict:
        return self._reassign(case_id, to_assignee, reason, actor, expected_version,
                              AssignmentKind.TRANSFER)

    def escalate_case(self, case_id: str, to_team: str, reason: str, actor: str,
                      expected_version: int) -> dict:
        return self._reassign(case_id, to_team, reason, actor, expected_version,
                              AssignmentKind.ESCALATION)

    def _reassign(self, case_id: str, to_party: str, reason: str, actor: str,
                  expected_version: int, kind: str) -> dict:
        """转派/升级共用逻辑：乐观锁保证并发下只有一个生效，失败尝试也留痕。"""
        if not to_party:
            raise ValidationError("必须指定接收方")
        if not reason:
            raise ValidationError("转派/升级必须说明原因")
        now = _iso(self._now())
        try:
            with self.store.write() as conn:
                case = self._get_case_row(conn, case_id)
                if case["status"] in TERMINAL_CASE_STATES:
                    raise ConflictError(f"案件已终结（{case['status']}），不能{kind}")
                if case["version"] != expected_version:
                    raise ConflictError(
                        f"案件版本已变化（期望 {expected_version}，实际 {case['version']}），"
                        "请刷新后重试")
                assignment_id = _id("asg")
                level = case["escalation_level"] + 1 if kind == AssignmentKind.ESCALATION else case["escalation_level"]
                conn.execute(
                    "INSERT INTO assignments (id, case_id, kind, from_party, to_party, reason, actor, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (assignment_id, case_id, kind, case["assignee"], to_party, reason, actor, now),
                )
                conn.execute(
                    "UPDATE cases SET assignee=?, escalation_level=?, version=version+1, updated_at=? WHERE id=?",
                    (to_party, level, now, case_id))
                ntf_kind = (NotificationKind.CASE_TRANSFERRED if kind == AssignmentKind.TRANSFER
                            else NotificationKind.CASE_ESCALATED)
                self._notify(conn, case_id, f"assignment:{assignment_id}", ntf_kind, to_party,
                             f"案件已{'转派' if kind == AssignmentKind.TRANSFER else '升级'}至 {to_party}")
                self._audit(conn, case_id, actor,
                            "CASE_TRANSFERRED" if kind == AssignmentKind.TRANSFER else "CASE_ESCALATED",
                            {"assignment_id": assignment_id, "from": case["assignee"],
                             "to": to_party, "reason": reason})
        except ConflictError as exc:
            # 失败的并发尝试同样写入责任链，便于事后追溯谁在何时尝试过
            if "版本已变化" in exc.message:
                with self.store.write() as conn:
                    self._get_case_row(conn, case_id)
                    self._audit(conn, case_id, actor, "REASSIGN_REJECTED",
                                {"kind": kind, "to": to_party, "reason": reason,
                                 "expected_version": expected_version})
            raise
        return self.get_case(case_id)

    # ------------------------------------------------------------ 时限
    def check_deadlines(self) -> list[dict]:
        """扫描到期时限，标记 BREACHED 并通知当前负责人。返回本次新违约的时限。"""
        now = _iso(self._now())
        breached = []
        with self.store.write() as conn:
            rows = conn.execute(
                "SELECT * FROM deadlines WHERE status = ? AND due_at < ?",
                (DeadlineStatus.PENDING, now)).fetchall()
            for row in rows:
                row = dict(row)
                conn.execute("UPDATE deadlines SET status=?, updated_at=? WHERE id=?",
                             (DeadlineStatus.BREACHED, now, row["id"]))
                if row["kind"] == "EVIDENCE":
                    conn.execute(
                        "UPDATE evidence_requests SET status=?, updated_at=?"
                        " WHERE case_id=? AND due_at=? AND status IN (?,?)",
                        (EvidenceRequestStatus.EXPIRED, now, row["case_id"], row["due_at"],
                         EvidenceRequestStatus.OPEN, EvidenceRequestStatus.PARTIAL))
                case = self._get_case_row(conn, row["case_id"])
                self._notify(conn, row["case_id"], f"deadline-breached:{row['id']}",
                             NotificationKind.DEADLINE_BREACHED, case["assignee"] or "unassigned",
                             f"时限 {row['kind']} 已超期")
                self._audit(conn, row["case_id"], "system", "DEADLINE_BREACHED",
                            {"deadline_id": row["id"], "kind": row["kind"], "due_at": row["due_at"]})
                breached.append(row)
        return breached

    def _mark_deadline_met(self, conn, case_id: str, kind: str):
        now = _iso(self._now())
        conn.execute(
            "UPDATE deadlines SET status=?, updated_at=? WHERE case_id=? AND kind=? AND status=?",
            (DeadlineStatus.MET, now, case_id, kind, DeadlineStatus.PENDING))

    # ------------------------------------------------------------ 结论与归档
    def resolve_case(self, case_id: str, resolution: str, note: str, actor: str) -> dict:
        valid = {Resolution.FULL_REFUND, Resolution.PARTIAL_REFUND,
                 Resolution.MERCHANT_LIABLE, Resolution.REJECTED}
        if resolution not in valid:
            raise ValidationError(f"结论必须是 {sorted(valid)} 之一")
        now = _iso(self._now())
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            ensure_case_transition(case["status"], CaseStatus.RESOLVED)
            open_reqs = conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_requests WHERE case_id=? AND status IN (?,?)",
                (case_id, EvidenceRequestStatus.OPEN, EvidenceRequestStatus.PARTIAL)).fetchone()["n"]
            if open_reqs:
                raise ConflictError(f"仍有 {open_reqs} 个证据请求未完成，不能出结论")
            settled = conn.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refunds WHERE case_id=? AND status=?",
                (case_id, RefundStatus.SETTLED)).fetchone()["s"]
            txn = conn.execute("SELECT amount_cents FROM transactions WHERE id=?",
                               (case["transaction_id"],)).fetchone()
            if resolution == Resolution.PARTIAL_REFUND and not (0 < settled < txn["amount_cents"]):
                raise ValidationError("部分退款结论要求已结算退款大于 0 且小于交易金额")
            if resolution == Resolution.FULL_REFUND and settled != txn["amount_cents"]:
                raise ValidationError("全额退款结论要求已结算退款等于交易金额")
            conn.execute("UPDATE cases SET status=?, resolution=?, resolution_note=?, updated_at=? WHERE id=?",
                         (CaseStatus.RESOLVED, resolution, note, now, case_id))
            self._notify(conn, case_id, f"resolved:{case_id}:{resolution}", NotificationKind.CASE_RESOLVED,
                         "customer", f"争议结论：{resolution}")
            self._audit(conn, case_id, actor, "CASE_RESOLVED",
                        {"resolution": resolution, "note": note, "settled_refund_cents": settled})
        return self.get_case(case_id)

    def close_case(self, case_id: str, actor: str) -> dict:
        now = _iso(self._now())
        with self.store.write() as conn:
            case = self._get_case_row(conn, case_id)
            ensure_case_transition(case["status"], CaseStatus.CLOSED)
            conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?",
                         (CaseStatus.CLOSED, now, case_id))
            self._audit(conn, case_id, actor, "CASE_CLOSED", {})
        return self.get_case(case_id)

    # ------------------------------------------------------------ 查询与导出
    def get_case_view(self, case_id: str, role: str | None = None) -> dict:
        """案件完整视图（按角色脱敏）：案件 + 交易 + 各子流程当前状态。"""
        case = self.get_case(case_id)
        txn = self.store.query_one("SELECT * FROM transactions WHERE id = ?", (case["transaction_id"],))
        auth = self.store.query_one(
            "SELECT * FROM authorizations WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,))
        view = {
            **case,
            "card_number": txn.get("card_number"),
            "amount_cents": txn["amount_cents"],
            "currency": txn["currency"],
            "merchant": txn.get("merchant"),
            "merchant_country": txn.get("merchant_country"),
            "occurred_at": txn.get("occurred_at"),
            "authorization": auth["status"] if auth else None,
            "evidence_requests": self.store.query(
                "SELECT * FROM evidence_requests WHERE case_id=? ORDER BY created_at", (case_id,)),
            "receipts": [self.get_receipt(r["id"]) for r in self.store.query(
                "SELECT id FROM receipts WHERE case_id=? ORDER BY received_at", (case_id,))],
            "refunds": self.store.query(
                "SELECT * FROM refunds WHERE case_id=? ORDER BY created_at", (case_id,)),
            "appeals": self.store.query(
                "SELECT * FROM appeals WHERE case_id=? ORDER BY created_at", (case_id,)),
            "translations": self.store.query(
                "SELECT * FROM translations WHERE case_id=? ORDER BY created_at", (case_id,)),
            "deadlines": self.store.query(
                "SELECT * FROM deadlines WHERE case_id=? ORDER BY created_at", (case_id,)),
            "assignments": self.store.query(
                "SELECT * FROM assignments WHERE case_id=? ORDER BY created_at", (case_id,)),
        }
        return mask_view(view, role)

    def list_notifications(self, case_id: str) -> list[dict]:
        return self.store.query(
            "SELECT * FROM notifications WHERE case_id=? ORDER BY created_at, id", (case_id,))

    def audit_trail(self, case_id: str) -> list[dict]:
        """审计导出：按序返回全部事件，detail 反序列化为对象。"""
        rows = self.store.query(
            "SELECT * FROM audit_log WHERE case_id=? ORDER BY seq", (case_id,))
        for r in rows:
            r["detail"] = loads(r["detail"])
        return rows

    def pending_external_requests(self) -> list[dict]:
        """未完成的外部请求：等待补证的请求 + 待处理的回执更正。

        进程重启后调用本方法即可恢复跟踪，不依赖内存状态。
        """
        reqs = self.store.query(
            "SELECT id, case_id, description, status, due_at, due_timezone FROM evidence_requests"
            " WHERE status IN (?, ?) ORDER BY created_at",
            (EvidenceRequestStatus.OPEN, EvidenceRequestStatus.PARTIAL))
        for r in reqs:
            r["type"] = "EVIDENCE_REQUEST"
        cors = self.store.query(
            "SELECT id, case_id, receipt_id, reason, status, created_at FROM receipt_corrections"
            " WHERE status = ? ORDER BY created_at",
            (CorrectionStatus.SUBMITTED,))
        for c in cors:
            c["type"] = "RECEIPT_CORRECTION"
        return reqs + cors
