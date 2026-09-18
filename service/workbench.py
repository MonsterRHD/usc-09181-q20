"""跨境争议客服工作台核心领域服务。

一个 Workbench 实例绑定一个 Database。所有跨表写操作在同一事务中提交；
外部系统交互通过可注入的 gateway 完成（默认真实网关只记录日志，测试可注入假网关）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Callable

from . import models as M
from .db import Database, row_to_dict
from .errors import (
    DuplicateReceipt,
    IllegalTransition,
    ImmutableReceipt,
    NotFound,
    OwnerChanged,
    ReceiptConflict,
    ValidationError,
)
from .masking import redact
from .timeutil import compute_deadline, is_past, now_utc, to_iso

# 各类回执默认通知的角色
RECEIPT_AUDIENCE = {
    M.RECEIPT_EVIDENCE_ACK: (M.ROLE_AGENT, M.ROLE_SUPERVISOR),
    M.RECEIPT_REFUND: (M.ROLE_AGENT, M.ROLE_SUPERVISOR, M.ROLE_MERCHANT),
    M.RECEIPT_CHARGEBACK: (M.ROLE_AGENT, M.ROLE_SUPERVISOR),
    M.RECEIPT_GENERIC: (M.ROLE_AGENT,),
}


def _hash_payload(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class ExternalGateway:
    """网关接口约定。真实实现可替换为 HTTP 客户端；此处只做抽象。

    send() 成功返回 True（请求已发出），失败抛异常。
    reconcile() 用于重启后核对 DISPATCHED 请求：返回
    'ACKNOWLEDGED' / 'DISPATCHED' / 'FAILED'。
    """

    def send(self, request: dict) -> bool:
        return True

    def reconcile(self, request: dict) -> str:
        return M.REQ_DISPATCHED


class Workbench:
    def __init__(
        self,
        db: Database,
        gateway: ExternalGateway | None = None,
        clock: Callable[[], object] | None = None,
    ):
        self.db = db
        self.gateway = gateway or ExternalGateway()
        self._clock = clock or now_utc

    # ------------------------------------------------------------------ #
    # 时间
    # ------------------------------------------------------------------ #
    def now(self):
        return self._clock()

    def now_iso(self) -> str:
        return to_iso(self.now())

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def audit(self, event_type: str, actor: str, detail: dict, case_id: int | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit_events(case_id,event_type,actor,detail,created_at)"
            " VALUES(?,?,?,?,?)",
            (case_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True),
             self.now_iso()),
        )

    # ------------------------------------------------------------------ #
    # 案件
    # ------------------------------------------------------------------ #
    def create_case(self, payload: dict, actor: str) -> dict:
        required = ["customer_name", "customer_email", "card_last4", "currency",
                    "amount_minor", "txn_time", "txn_timezone", "merchant_name",
                    "acquirer_ref", "reason_code", "txn_ref"]
        missing = [k for k in required if payload.get(k) in (None, "")]
        if missing:
            raise ValidationError(f"缺少必填字段: {','.join(missing)}")
        if len(str(payload["card_last4"])) != 4 or not str(payload["card_last4"]).isdigit():
            raise ValidationError("card_last4 必须为 4 位数字（禁止存储完整卡号）")

        now = self.now_iso()
        with self.db.lock:
            # 重复申诉：同一原始交易 + 同一争议码，且已有未终结案件 → 关联，不另立案
            dup = self.db.query_one(
                "SELECT * FROM cases WHERE txn_ref=? AND reason_code=? "
                "AND status NOT IN ('RESOLVED') ORDER BY id DESC LIMIT 1",
                (payload["txn_ref"], payload["reason_code"]),
            )
            case_ref = _new_ref("CASE")
            cur = self.db.execute(
                "INSERT INTO cases(case_ref,status,resolution,txn_ref,customer_name,"
                "customer_email,card_last4,currency,amount_minor,txn_time,txn_timezone,"
                "merchant_name,acquirer_ref,reason_code,duplicate_of,current_owner,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_ref, M.CASE_OPEN, None, payload["txn_ref"], payload["customer_name"],
                 payload["customer_email"], payload["card_last4"], payload["currency"],
                 int(payload["amount_minor"]), payload["txn_time"], payload["txn_timezone"],
                 payload["merchant_name"], payload["acquirer_ref"], payload["reason_code"],
                 row_to_dict(dup)["case_ref"] if dup else None,
                 actor, now, now),
            )
            case_id = cur.lastrowid
            self.db.execute(
                "INSERT INTO handovers(case_id,chain_seq,kind,from_owner,to_owner,reason,"
                "actor,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (case_id, 1, M.HANDOVER_TRANSFER, "-", actor, "案件建立，初始责任人", actor, now),
            )
            self.audit("CASE_CREATED", actor, {
                "case_ref": case_ref,
                "duplicate_of": row_to_dict(dup)["case_ref"] if dup else None,
                "txn_ref": payload["txn_ref"],
            }, case_id)
            self.db.commit()
        result = self.get_case(case_ref)
        result["duplicate"] = dup is not None
        return result

    def get_case(self, case_ref: str, role: str = M.ROLE_SUPERVISOR) -> dict:
        row = self.db.query_one("SELECT * FROM cases WHERE case_ref=?", (case_ref,))
        if not row:
            raise NotFound(f"案件不存在: {case_ref}")
        case = row_to_dict(row)
        case_id = case["id"]
        case["evidence_requests"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM evidence_requests WHERE case_id=? ORDER BY id", (case_id,))
        ]
        case["refunds"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM refunds WHERE case_id=? ORDER BY id", (case_id,))
        ]
        case["translations"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM translations WHERE case_id=? ORDER BY version", (case_id,))
        ]
        case["withdrawal"] = row_to_dict(self.db.query_one(
            "SELECT * FROM withdrawals WHERE case_id=? ORDER BY id DESC LIMIT 1", (case_id,)))
        case["receipts"] = [
            self._receipt_view(r["id"], role) for r in self.db.query_all(
                "SELECT id FROM bank_receipts WHERE case_id=? ORDER BY id", (case_id,))
        ]
        case["deadlines"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM deadlines WHERE case_id=? ORDER BY due_at", (case_id,))
        ]
        case["responsibility_chain"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM handovers WHERE case_id=? ORDER BY chain_seq", (case_id,))
        ]
        case["external_requests"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT id,request_uid,target_party,request_type,status,attempts,"
                "idempotency_key,linked_receipt_uid,last_error,created_at,updated_at "
                "FROM external_requests WHERE case_id=? ORDER BY id", (case_id,))
        ]
        return redact(case, role)

    def list_cases(self, role: str = M.ROLE_SUPERVISOR) -> list[dict]:
        rows = self.db.query_all("SELECT case_ref FROM cases ORDER BY id")
        return [self.get_case(r["case_ref"], role) for r in rows]

    def _set_status(self, case_id: int, status: str, resolution: str | None = None) -> None:
        self.db.execute(
            "UPDATE cases SET status=?, resolution=COALESCE(?,resolution), updated_at=? "
            "WHERE id=?",
            (status, resolution, self.now_iso(), case_id),
        )

    def _case_row(self, case_ref: str):
        row = self.db.query_one("SELECT * FROM cases WHERE case_ref=?", (case_ref,))
        if not row:
            raise NotFound(f"案件不存在: {case_ref}")
        return row

    # ------------------------------------------------------------------ #
    # 转派与升级（责任链 + 乐观并发）
    # ------------------------------------------------------------------ #
    def transfer_case(self, case_ref: str, to_owner: str, reason: str, actor: str,
                      *, expected_owner: str | None = None, escalate: bool = False) -> dict:
        if not reason:
            raise ValidationError("转派必须填写原因")
        now = self.now_iso()
        with self.db.lock:
            row = self._case_row(case_ref)
            case = row_to_dict(row)
            if case["status"] in M.TERMINAL_CASE_STATUSES:
                raise IllegalTransition(f"案件已终结（{case['status']}），不能转派")
            if expected_owner is not None and case["current_owner"] != expected_owner:
                # 并发转派冲突：责任人已被其他人改走
                raise OwnerChanged(
                    f"责任人已变更：期望 {expected_owner}，实际 {case['current_owner']}")
            kind = M.HANDOVER_ESCALATION if escalate else M.HANDOVER_TRANSFER
            seq = self.db.query_one(
                "SELECT COALESCE(MAX(chain_seq),0)+1 AS next FROM handovers WHERE case_id=?",
                (case["id"],))["next"]
            self.db.execute(
                "INSERT INTO handovers(case_id,chain_seq,kind,from_owner,to_owner,reason,"
                "actor,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (case["id"], seq, kind, case["current_owner"], to_owner, reason, actor, now))
            new_status = M.CASE_ESCALATED if escalate else case["status"]
            self.db.execute(
                "UPDATE cases SET current_owner=?, status=?, updated_at=? WHERE id=?",
                (to_owner, new_status, now, case["id"]))
            self.audit("CASE_ESCALATED" if escalate else "CASE_TRANSFERRED", actor, {
                "from": case["current_owner"], "to": to_owner, "reason": reason,
                "chain_seq": seq,
            }, case["id"])
            self.db.commit()
        return self.get_case(case_ref)

    # ------------------------------------------------------------------ #
    # 客户授权
    # ------------------------------------------------------------------ #
    def record_authorization(self, case_ref: str, kind: str, granted: bool, actor: str,
                             *, auth_ref: str | None = None, window_hours: float | None = None,
                             snapshot: dict | None = None) -> dict:
        row = self._case_row(case_ref)
        case = row_to_dict(row)
        now = self.now()
        auth_ref = auth_ref or _new_ref("AUTH")
        window_start = to_iso(now)
        window_end = to_iso(now) if window_hours is None else None
        if window_hours is not None:
            from datetime import timedelta
            window_end = to_iso(now + timedelta(hours=window_hours))
        snap = json.dumps(snapshot or {
            "txn_ref": case["txn_ref"], "amount_minor": case["amount_minor"],
            "currency": case["currency"], "status_at_auth": case["status"],
        }, ensure_ascii=False, sort_keys=True)
        with self.db.lock:
            self.db.execute(
                "INSERT INTO authorizations(case_id,auth_ref,kind,granted,granted_at,"
                "window_start,window_end,snapshot,revoked,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,0,?)",
                (case["id"], auth_ref, kind, 1 if granted else 0,
                 self.now_iso() if granted else None, window_start, window_end,
                 snap, self.now_iso()))
            self.audit("AUTHORIZATION_RECORDED", actor, {
                "auth_ref": auth_ref, "kind": kind, "granted": granted,
            }, case["id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM authorizations WHERE auth_ref=?", (auth_ref,)))

    def revoke_authorization(self, auth_ref: str, actor: str) -> dict:
        row = self.db.query_one("SELECT * FROM authorizations WHERE auth_ref=?", (auth_ref,))
        if not row:
            raise NotFound(f"授权不存在: {auth_ref}")
        self.db.execute("UPDATE authorizations SET revoked=1 WHERE id=?", (row["id"],))
        self.audit("AUTHORIZATION_REVOKED", actor, {"auth_ref": auth_ref}, row["case_id"])
        self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM authorizations WHERE auth_ref=?", (auth_ref,)))

    # ------------------------------------------------------------------ #
    # 证据请求与补证
    # ------------------------------------------------------------------ #
    def request_evidence(self, case_ref: str, target_party: str, description: str,
                         actor: str, *, deadline_hours: float = 72.0,
                         timezone_name: str = "UTC", request_ref: str | None = None) -> dict:
        if target_party not in M.EXTERNAL_PARTIES:
            raise ValidationError(f"证据请求对象必须是外部参与方: {target_party}")
        case = row_to_dict(self._case_row(case_ref))
        if case["status"] in M.TERMINAL_CASE_STATUSES:
            raise IllegalTransition("案件已终结")
        due_iso, label = compute_deadline(
            deadline_hours, origin=self.now(), timezone_name=timezone_name)
        request_ref = request_ref or _new_ref("EVD")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "INSERT INTO evidence_requests(case_id,request_ref,target_party,description,"
                "status,deadline,deadline_label,requested_at) VALUES(?,?,?,?,?,?,?,?)",
                (case["id"], request_ref, target_party, description, M.EV_REQUESTED,
                 due_iso, label, now))
            self.db.execute(
                "INSERT INTO deadlines(case_id,subject_type,subject_ref,due_at,due_label,"
                "status,created_at) VALUES(?,?,?,?,?,?,?)",
                (case["id"], "EVIDENCE", request_ref, due_iso, label, M.DL_OPEN, now))
            if case["status"] == M.CASE_OPEN:
                self._set_status(case["id"], M.CASE_EVIDENCE_REQUESTED)
            ext_uid = self._create_external_request(
                case["id"], target_party, "EVIDENCE_REQUEST",
                {"request_ref": request_ref, "description": description, "deadline": due_iso},
                idem_key=f"EVREQ:{request_ref}")
            self.audit("EVIDENCE_REQUESTED", actor, {
                "request_ref": request_ref, "target_party": target_party,
                "deadline": label, "external_uid": ext_uid,
            }, case["id"])
            self.db.commit()
        self._dispatch_pending(case["id"])
        return row_to_dict(self.db.query_one(
            "SELECT * FROM evidence_requests WHERE request_ref=?", (request_ref,)))

    def submit_evidence(self, request_ref: str, evidence_summary: str, complete: bool,
                        missing_items: list[str], actor: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM evidence_requests WHERE request_ref=?", (request_ref,))
        if not row:
            raise NotFound(f"证据请求不存在: {request_ref}")
        req = row_to_dict(row)
        if req["status"] != M.EV_REQUESTED:
            raise IllegalTransition(f"证据请求当前状态 {req['status']}，不能重复提交")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "UPDATE evidence_requests SET status=?, evidence_summary=?, completeness=?,"
                "missing_items=?, submitted_at=? WHERE id=?",
                (M.EV_SUBMITTED, evidence_summary,
                 "COMPLETE" if complete else "INCOMPLETE",
                 json.dumps(missing_items, ensure_ascii=False), now, req["id"]))
            # 在截止前提交 → 时限达成；逾期仍提交但时限已由 sweep 标记 BREACHED
            if not is_past(req["deadline"], now=self.now()):
                self.db.execute(
                    "UPDATE deadlines SET status=?, closed_at=? WHERE subject_type='EVIDENCE'"
                    " AND subject_ref=? AND status='OPEN'",
                    (M.DL_MET, now, request_ref))
            self.audit("EVIDENCE_SUBMITTED", actor, {
                "request_ref": request_ref, "complete": complete,
                "missing_items": missing_items,
            }, req["case_id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM evidence_requests WHERE request_ref=?", (request_ref,)))

    def decide_evidence(self, request_ref: str, accept: bool, note: str, actor: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM evidence_requests WHERE request_ref=?", (request_ref,))
        if not row:
            raise NotFound(f"证据请求不存在: {request_ref}")
        req = row_to_dict(row)
        if req["status"] != M.EV_SUBMITTED:
            raise IllegalTransition(f"证据尚未提交或已裁决（{req['status']}）")
        now = self.now_iso()
        with self.db.lock:
            status = M.EV_ACCEPTED if accept else M.EV_REJECTED
            self.db.execute(
                "UPDATE evidence_requests SET status=?, decided_at=?, decided_by=? WHERE id=?",
                (status, now, actor, req["id"]))
            if accept:
                self._set_status(req["case_id"], M.CASE_IN_REVIEW)
            else:
                # 证据不足被驳回 → 重新请求补证：旧请求留痕，另开新请求由调用方发起
                self.db.execute(
                    "UPDATE deadlines SET status=?, closed_at=? WHERE subject_type='EVIDENCE'"
                    " AND subject_ref=? AND status='OPEN'",
                    (M.DL_MET, now, request_ref))
            self.audit("EVIDENCE_DECIDED", actor, {
                "request_ref": request_ref, "accepted": accept, "note": note,
            }, req["case_id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM evidence_requests WHERE request_ref=?", (request_ref,)))

    # ------------------------------------------------------------------ #
    # 部分退款
    # ------------------------------------------------------------------ #
    def propose_refund(self, case_ref: str, amount_minor: int, currency: str, reason: str,
                       actor: str, *, kind: str = M.REFUND_KIND_PARTIAL) -> dict:
        case = row_to_dict(self._case_row(case_ref))
        if case["status"] in M.TERMINAL_CASE_STATUSES:
            raise IllegalTransition("案件已终结")
        if amount_minor <= 0:
            raise ValidationError("退款金额必须为正")
        if amount_minor > case["amount_minor"]:
            raise ValidationError("退款金额不能超过原交易金额（超额请走全额退款/其他流程）")
        refund_ref = _new_ref("RFD")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "INSERT INTO refunds(case_id,refund_ref,kind,currency,amount_minor,status,"
                "reason,proposed_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (case["id"], refund_ref, kind, currency, amount_minor, M.RF_PROPOSED,
                 reason, actor, now, now))
            self.audit("REFUND_PROPOSED", actor, {
                "refund_ref": refund_ref, "amount_minor": amount_minor,
                "currency": currency, "kind": kind, "reason": reason,
            }, case["id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM refunds WHERE refund_ref=?", (refund_ref,)))

    def approve_refund(self, refund_ref: str, actor: str) -> dict:
        row = self.db.query_one("SELECT * FROM refunds WHERE refund_ref=?", (refund_ref,))
        if not row:
            raise NotFound(f"退款不存在: {refund_ref}")
        refund = row_to_dict(row)
        if refund["status"] != M.RF_PROPOSED:
            raise IllegalTransition(f"退款当前状态 {refund['status']}")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "UPDATE refunds SET status=?, approved_by=?, updated_at=? WHERE id=?",
                (M.RF_APPROVED, actor, now, refund["id"]))
            self.audit("REFUND_APPROVED", actor, {"refund_ref": refund_ref}, refund["case_id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM refunds WHERE refund_ref=?", (refund_ref,)))

    def send_refund_to_bank(self, refund_ref: str, actor: str) -> dict:
        row = self.db.query_one("SELECT * FROM refunds WHERE refund_ref=?", (refund_ref,))
        if not row:
            raise NotFound(f"退款不存在: {refund_ref}")
        refund = row_to_dict(row)
        if refund["status"] != M.RF_APPROVED:
            raise IllegalTransition(f"退款未经审批（当前 {refund['status']}），不能发送银行")
        with self.db.lock:
            ext_uid = self._create_external_request(
                refund["case_id"], M.PARTY_ACQUIRER, "REFUND",
                {"refund_ref": refund_ref, "amount_minor": refund["amount_minor"],
                 "currency": refund["currency"]},
                idem_key=f"REFUND:{refund_ref}")
            self.db.execute(
                "UPDATE refunds SET status=?, updated_at=? WHERE id=?",
                (M.RF_SENT_TO_BANK, self.now_iso(), refund["id"]))
            self.audit("REFUND_SENT_TO_BANK", actor, {
                "refund_ref": refund_ref, "external_uid": ext_uid,
            }, refund["case_id"])
            self.db.commit()
        self._dispatch_pending(refund["case_id"])
        return row_to_dict(self.db.query_one(
            "SELECT * FROM refunds WHERE refund_ref=?", (refund_ref,)))

    def _confirm_refund_by_receipt(self, case_id: int, receipt: dict) -> None:
        """银行 REFUND 回执到达：按金额与币种匹配一笔 SENT_TO_BANK 退款。"""
        for r in self.db.query_all(
                "SELECT * FROM refunds WHERE case_id=? AND status=? ORDER BY id",
                (case_id, M.RF_SENT_TO_BANK)):
            refund = row_to_dict(r)
            if (receipt.get("amount_minor") is None or
                    (refund["amount_minor"] == receipt["amount_minor"]
                     and (not receipt.get("currency") or refund["currency"] == receipt["currency"]))):
                self.db.execute(
                    "UPDATE refunds SET status=?, linked_receipt_uid=?, updated_at=? WHERE id=?",
                    (M.RF_CONFIRMED, receipt["receipt_uid"], self.now_iso(), refund["id"]))
                self.audit("REFUND_CONFIRMED", "SYSTEM", {
                    "refund_ref": refund["refund_ref"],
                    "receipt_uid": receipt["receipt_uid"],
                }, case_id)
                return

    # ------------------------------------------------------------------ #
    # 翻译版本
    # ------------------------------------------------------------------ #
    def request_translation(self, case_ref: str, source_lang: str, target_lang: str,
                            source_text: str, actor: str) -> dict:
        case = row_to_dict(self._case_row(case_ref))
        now = self.now_iso()
        with self.db.lock:
            seq_row = self.db.query_one(
                "SELECT COALESCE(MAX(version),0)+1 AS next FROM translations WHERE case_id=?",
                (case["id"],))
            version = seq_row["next"]
            cur = self.db.execute(
                "INSERT INTO translations(case_id,version,source_lang,target_lang,source_text,"
                "status,requested_at) VALUES(?,?,?,?,?,?,?)",
                (case["id"], version, source_lang, target_lang, source_text,
                 M.TR_REQUESTED, now))
            translation_id = cur.lastrowid
            ext_uid = self._create_external_request(
                case["id"], M.PARTY_TRANSLATOR, "TRANSLATION",
                {"translation_id": translation_id, "source_lang": source_lang,
                 "target_lang": target_lang},
                idem_key=f"TRANS:{case['id']}:{version}")
            self.audit("TRANSLATION_REQUESTED", actor, {
                "version": version, "target_lang": target_lang,
                "external_uid": ext_uid,
            }, case["id"])
            self.db.commit()
        self._dispatch_pending(case["id"])
        return row_to_dict(self.db.query_one(
            "SELECT * FROM translations WHERE id=?", (translation_id,)))

    def deliver_translation(self, case_ref: str, version: int, translated_text: str,
                            translator: str, actor: str) -> dict:
        case = row_to_dict(self._case_row(case_ref))
        row = self.db.query_one(
            "SELECT * FROM translations WHERE case_id=? AND version=?",
            (case["id"], version))
        if not row:
            raise NotFound(f"翻译版本不存在: 案件 {case_ref} v{version}")
        now = self.now_iso()
        with self.db.lock:
            # 旧版本一律标记 SUPERSEDED，不覆盖译文内容
            self.db.execute(
                "UPDATE translations SET status=? WHERE case_id=? AND status=? AND id<>?",
                (M.TR_SUPERSEDED, case["id"], M.TR_DELIVERED, row["id"]))
            self.db.execute(
                "UPDATE translations SET translated_text=?, status=?, translator=?,"
                "delivered_at=? WHERE id=?",
                (translated_text, M.TR_DELIVERED, translator, now, row["id"]))
            self.audit("TRANSLATION_DELIVERED", actor, {
                "version": version, "translator": translator,
            }, case["id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM translations WHERE id=?", (row["id"],)))

    # ------------------------------------------------------------------ #
    # 客户撤回
    # ------------------------------------------------------------------ #
    def request_withdrawal(self, case_ref: str, reason: str, actor: str) -> dict:
        case = row_to_dict(self._case_row(case_ref))
        if case["status"] in M.TERMINAL_CASE_STATUSES:
            raise IllegalTransition("案件已终结")
        existing = self.db.query_one(
            "SELECT * FROM withdrawals WHERE case_id=? AND status=?",
            (case["id"], M.WD_REQUESTED))
        if existing:
            raise IllegalTransition("已存在待处理的撤回请求")
        ref = _new_ref("WD")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "INSERT INTO withdrawals(case_id,withdrawal_ref,status,reason,requested_at)"
                " VALUES(?,?,?,?,?)",
                (case["id"], ref, M.WD_REQUESTED, reason, now))
            self.audit("WITHDRAWAL_REQUESTED", actor, {"withdrawal_ref": ref}, case["id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM withdrawals WHERE withdrawal_ref=?", (ref,)))

    def confirm_withdrawal(self, withdrawal_ref: str, actor: str) -> dict:
        row = self.db.query_one(
            "SELECT * FROM withdrawals WHERE withdrawal_ref=?", (withdrawal_ref,))
        if not row:
            raise NotFound(f"撤回请求不存在: {withdrawal_ref}")
        wd = row_to_dict(row)
        if wd["status"] != M.WD_REQUESTED:
            raise IllegalTransition(f"撤回请求状态 {wd['status']}")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "UPDATE withdrawals SET status=?, confirmed_at=?, processed_by=? WHERE id=?",
                (M.WD_CONFIRMED, now, actor, wd["id"]))
            self._set_status(wd["case_id"], M.CASE_RESOLVED, M.RESOLUTION_WITHDRAWN)
            self._close_open_deadlines(wd["case_id"], now)
            self.audit("WITHDRAWAL_CONFIRMED", actor, {
                "withdrawal_ref": withdrawal_ref,
            }, wd["case_id"])
            self.db.commit()
        return row_to_dict(self.db.query_one(
            "SELECT * FROM withdrawals WHERE withdrawal_ref=?", (withdrawal_ref,)))

    # ------------------------------------------------------------------ #
    # 最终结论
    # ------------------------------------------------------------------ #
    def resolve_case(self, case_ref: str, resolution: str, actor: str, note: str) -> dict:
        if resolution not in (M.RESOLUTION_UPHELD, M.RESOLUTION_REJECTED,
                              M.RESOLUTION_PARTIAL_REFUND):
            raise ValidationError(f"未知结论: {resolution}")
        case = row_to_dict(self._case_row(case_ref))
        if case["status"] in M.TERMINAL_CASE_STATUSES:
            raise IllegalTransition("案件已终结")
        if resolution == M.RESOLUTION_PARTIAL_REFUND:
            confirmed = self.db.query_one(
                "SELECT 1 FROM refunds WHERE case_id=? AND status=?",
                (case["id"], M.RF_CONFIRMED))
            if not confirmed:
                raise IllegalTransition("部分退款结论需要一笔银行已确认的退款回执")
        now = self.now_iso()
        with self.db.lock:
            self._set_status(case["id"], M.CASE_RESOLVED, resolution)
            self._close_open_deadlines(case["id"], now)
            self.audit("CASE_RESOLVED", actor, {
                "resolution": resolution, "note": note,
            }, case["id"])
            self.db.commit()
        return self.get_case(case_ref)

    # ------------------------------------------------------------------ #
    # 银行原始回执（只追加 + 幂等 + 通知去重）
    # ------------------------------------------------------------------ #
    def _receipt_view(self, receipt_id: int, role: str = M.ROLE_SUPERVISOR) -> dict:
        row = self.db.query_one("SELECT * FROM bank_receipts WHERE id=?", (receipt_id,))
        item = row_to_dict(row)
        # 原始回执内容按角色呈现：JSON 则按敏感字段脱敏；
        # 非结构化原文仅对客服/主管/收单行可见，翻译等最小权限角色遮蔽。
        try:
            item["payload_text"] = json.dumps(
                redact(json.loads(item["payload_text"]), role),
                ensure_ascii=False, sort_keys=True)
        except (ValueError, TypeError):
            if role not in (M.ROLE_SUPERVISOR, M.ROLE_AGENT, M.ROLE_BANK):
                item["payload_text"] = "***"
        item["corrections"] = [
            row_to_dict(r) for r in self.db.query_all(
                "SELECT id,reason,corrected_payload,operator,created_at "
                "FROM receipt_corrections WHERE receipt_id=? ORDER BY id", (receipt_id,))
        ]
        for c in item["corrections"]:
            try:
                c["corrected_payload"] = redact(json.loads(c["corrected_payload"]), role)
            except (ValueError, TypeError):
                pass
        return item

    def receive_bank_receipt(self, case_ref: str, receipt_uid: str, receipt_type: str,
                             payload_text: str, actor: str = "BANK-GATEWAY",
                             *, event_time: str | None = None, currency: str | None = None,
                             amount_minor: int | None = None,
                             channel: str = "WORKBENCH") -> dict:
        case = row_to_dict(self._case_row(case_ref))
        new_hash = _hash_payload(payload_text)
        now = self.now_iso()

        # 幂等：相同回执编号重复到达
        existing = self.db.query_one(
            "SELECT * FROM bank_receipts WHERE receipt_uid=?", (receipt_uid,))
        if existing:
            if existing["payload_hash"] != new_hash:
                raise ReceiptConflict(
                    f"回执编号 {receipt_uid} 已存在但内容不一致，拒绝覆盖原始回执")
            self.audit("RECEIPT_DUPLICATE_IGNORED", actor, {
                "receipt_uid": receipt_uid,
            }, existing["case_id"])
            self.db.commit()
            raise DuplicateReceipt(f"重复回执 {receipt_uid}，已忽略且不重复通知")

        with self.db.lock:
            cur = self.db.execute(
                "INSERT INTO bank_receipts(case_id,receipt_uid,receipt_type,payload_hash,"
                "payload_text,currency,amount_minor,event_time,received_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (case["id"], receipt_uid, receipt_type, new_hash, payload_text,
                 currency, amount_minor, event_time or now, now))
            receipt_id = cur.lastrowid
            receipt = {
                "id": receipt_id, "case_id": case["id"], "receipt_uid": receipt_uid,
                "receipt_type": receipt_type, "payload_hash": new_hash,
                "payload_text": payload_text, "currency": currency,
                "amount_minor": amount_minor, "event_time": event_time or now,
                "received_at": now,
            }
            # 回执可能关联在途外部请求（通过 payload 中的 request_uid 或退款匹配）
            self._ack_request_by_receipt(receipt)
            if receipt_type == M.RECEIPT_REFUND:
                self._confirm_refund_by_receipt(case["id"], receipt)
            notified_roles = self._notify_receipt(case, receipt, channel)
            self.audit("RECEIPT_RECEIVED", actor, {
                "receipt_uid": receipt_uid, "receipt_type": receipt_type,
                "event_time": receipt["event_time"],
                "notified_roles": notified_roles,
            }, case["id"])
            self.db.commit()
        return self._receipt_view(receipt_id)

    def _ack_request_by_receipt(self, receipt: dict) -> None:
        request_uid = None
        try:
            data = json.loads(receipt["payload_text"])
            if isinstance(data, dict):
                request_uid = data.get("request_uid")
        except (ValueError, TypeError):
            data = None
        if request_uid:
            row = self.db.query_one(
                "SELECT * FROM external_requests WHERE request_uid=?", (request_uid,))
            if row and row["status"] in M.UNFINISHED_REQUEST_STATUSES:
                self.db.execute(
                    "UPDATE external_requests SET status=?, linked_receipt_uid=?, "
                    "updated_at=? WHERE id=?",
                    (M.REQ_ACKNOWLEDGED, receipt["receipt_uid"],
                     self.now_iso(), row["id"]))
                self.audit("EXTERNAL_REQUEST_ACKED", "SYSTEM", {
                    "request_uid": request_uid, "receipt_uid": receipt["receipt_uid"],
                }, row["case_id"])

    def _notify_receipt(self, case: dict, receipt: dict, channel: str) -> list[str]:
        """按角色生成脱敏通知；UNIQUE(receipt_uid,channel,target_role) 保证去重。"""
        roles = RECEIPT_AUDIENCE.get(receipt["receipt_type"], (M.ROLE_AGENT,))
        subject = f"[{receipt['receipt_type']}] 案件 {case['case_ref']} 新回执"
        delivered_to: list[str] = []
        now = self.now_iso()
        for role in roles:
            view = redact({
                "case_ref": case["case_ref"],
                "receipt_uid": receipt["receipt_uid"],
                "receipt_type": receipt["receipt_type"],
                "customer_name": case["customer_name"],
                "customer_email": case["customer_email"],
                "card_last4": case["card_last4"],
                "amount_minor": receipt.get("amount_minor"),
                "currency": receipt.get("currency"),
                "event_time": receipt.get("event_time"),
            }, role)
            body = json.dumps(view, ensure_ascii=False, sort_keys=True)
            try:
                self.db.execute(
                    "INSERT INTO notifications(case_id,receipt_uid,channel,target_role,"
                    "subject,body,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (case["id"], receipt["receipt_uid"], channel, role, subject, body,
                     M.NTF_PENDING, now))
                delivered_to.append(role)
            except Exception:
                # UNIQUE 冲突 = 该角色已收到过此回执；不重复通知
                pass
        return delivered_to

    def deliver_pending_notifications(self) -> int:
        """把待发通知标记为已送达（真实环境对接推送通道）。返回送达数量。"""
        rows = self.db.query_all(
            "SELECT id FROM notifications WHERE status=?", (M.NTF_PENDING,))
        now = self.now_iso()
        for r in rows:
            self.db.execute(
                "UPDATE notifications SET status=?, delivered_at=? WHERE id=?",
                (M.NTF_DELIVERED, now, r["id"]))
        self.db.commit()
        return len(rows)

    def list_notifications(self, role: str) -> list[dict]:
        return [row_to_dict(r) for r in self.db.query_all(
            "SELECT * FROM notifications WHERE target_role=? ORDER BY id", (role,))]

    # ------------------------------------------------------------------ #
    # 银行回执更正（原始回执不可改，只能追加带原因的更正）
    # ------------------------------------------------------------------ #
    def correct_receipt(self, receipt_uid: str, corrected_payload: dict, reason: str,
                        operator: str) -> dict:
        if not reason:
            raise ValidationError("更正必须填写原因")
        row = self.db.query_one(
            "SELECT * FROM bank_receipts WHERE receipt_uid=?", (receipt_uid,))
        if not row:
            raise NotFound(f"回执不存在: {receipt_uid}")
        now = self.now_iso()
        with self.db.lock:
            self.db.execute(
                "INSERT INTO receipt_corrections(receipt_id,reason,corrected_payload,"
                "operator,created_at) VALUES(?,?,?,?,?)",
                (row["id"], reason,
                 json.dumps(corrected_payload, ensure_ascii=False, sort_keys=True),
                 operator, now))
            self.audit("RECEIPT_CORRECTED", operator, {
                "receipt_uid": receipt_uid, "reason": reason,
                "corrected": corrected_payload,
            }, row["case_id"])
            self.db.commit()
        return self._receipt_view(row["id"])

    def update_receipt_payload(self, *_args, **_kwargs):
        # 显式封堵：任何“修改原始回执”的尝试都被拒绝
        raise ImmutableReceipt("银行原始回执不可修改，请使用 correct_receipt 追加带原因的更正")

    # ------------------------------------------------------------------ #
    # 外部请求：发送、失败重试与重启恢复
    # ------------------------------------------------------------------ #
    def _create_external_request(self, case_id: int, target_party: str, request_type: str,
                                 payload: dict, *, idem_key: str) -> str:
        request_uid = _new_ref("REQ")
        self.db.execute(
            "INSERT INTO external_requests(case_id,request_uid,target_party,request_type,"
            "payload,status,attempts,idempotency_key,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,0,?,?,?)",
            (case_id, request_uid, target_party, request_type,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             M.REQ_PENDING, idem_key, self.now_iso(), self.now_iso()))
        return request_uid

    def _dispatch_pending(self, case_id: int | None = None) -> dict[str, int]:
        """发送所有 PENDING 外部请求；成功置 DISPATCHED，失败记录错误并保持 PENDING。"""
        sql = "SELECT * FROM external_requests WHERE status=?"
        params: tuple = (M.REQ_PENDING,)
        if case_id is not None:
            sql += " AND case_id=?"
            params = (M.REQ_PENDING, case_id)
        sent = failed = 0
        for r in self.db.query_all(sql, params):
            req = row_to_dict(r)
            try:
                ok = self.gateway.send(req)
                if not ok:
                    raise RuntimeError("网关拒绝发送")
                self.db.execute(
                    "UPDATE external_requests SET status=?, attempts=attempts+1, "
                    "updated_at=? WHERE id=?",
                    (M.REQ_DISPATCHED, self.now_iso(), req["id"]))
                self.audit("EXTERNAL_REQUEST_DISPATCHED", "SYSTEM", {
                    "request_uid": req["request_uid"],
                    "idempotency_key": req["idempotency_key"],
                }, req["case_id"])
                sent += 1
            except Exception as exc:  # 网络失败：保持 PENDING，等待恢复/重试
                self.db.execute(
                    "UPDATE external_requests SET attempts=attempts+1, last_error=?, "
                    "updated_at=? WHERE id=?",
                    (str(exc), self.now_iso(), req["id"]))
                self.audit("EXTERNAL_REQUEST_FAILED", "SYSTEM", {
                    "request_uid": req["request_uid"], "error": str(exc),
                }, req["case_id"])
                failed += 1
        self.db.commit()
        return {"sent": sent, "failed": failed}

    def list_unfinished_requests(self) -> list[dict]:
        placeholders = ",".join("?" for _ in M.UNFINISHED_REQUEST_STATUSES)
        rows = self.db.query_all(
            f"SELECT * FROM external_requests WHERE status IN ({placeholders}) ORDER BY id",
            tuple(M.UNFINISHED_REQUEST_STATUSES))
        return [row_to_dict(r) for r in rows]

    def recover_external_requests(self) -> dict:
        """程序再次运行后调用：PENDING 重发，DISPATCHED 向外部核对回执。"""
        redispatched = acknowledged = failed = 0
        for req in self.list_unfinished_requests():
            if req["status"] == M.REQ_PENDING:
                try:
                    self.gateway.send(req)
                    self.db.execute(
                        "UPDATE external_requests SET status=?, attempts=attempts+1,"
                        " updated_at=? WHERE id=?",
                        (M.REQ_DISPATCHED, self.now_iso(), req["id"]))
                    redispatched += 1
                except Exception as exc:
                    self.db.execute(
                        "UPDATE external_requests SET attempts=attempts+1, last_error=?,"
                        " updated_at=? WHERE id=?",
                        (str(exc), self.now_iso(), req["id"]))
                    failed += 1
            elif req["status"] == M.REQ_DISPATCHED:
                outcome = self.gateway.reconcile(req)
                if outcome == M.REQ_ACKNOWLEDGED:
                    self.db.execute(
                        "UPDATE external_requests SET status=?, updated_at=? WHERE id=?",
                        (M.REQ_ACKNOWLEDGED, self.now_iso(), req["id"]))
                    acknowledged += 1
                elif outcome == M.REQ_FAILED:
                    self.db.execute(
                        "UPDATE external_requests SET status=?, updated_at=? WHERE id=?",
                        (M.REQ_FAILED, self.now_iso(), req["id"]))
                    failed += 1
                # 仍 DISPATCHED：继续可追踪，等待回执
            self.audit("EXTERNAL_REQUEST_RECOVERY", "SYSTEM", {
                "request_uid": req["request_uid"], "status_after": req["status"],
            }, req["case_id"])
        self.db.commit()
        return {"redispatched": redispatched, "acknowledged": acknowledged,
                "failed": failed, "still_pending": len(self.list_unfinished_requests())}

    # ------------------------------------------------------------------ #
    # 时限
    # ------------------------------------------------------------------ #
    def _close_open_deadlines(self, case_id: int, now: str) -> None:
        self.db.execute(
            "UPDATE deadlines SET status=?, closed_at=? WHERE case_id=? AND status='OPEN'",
            (M.DL_MET, now, case_id))

    def sweep_deadlines(self) -> dict:
        """将已过 UTC 截止时刻且仍 OPEN 的时限标记为 BREACHED。"""
        now_dt = self.now()
        breached: list[str] = []
        for r in self.db.query_all(
                "SELECT * FROM deadlines WHERE status=?", (M.DL_OPEN,)):
            if is_past(r["due_at"], now=now_dt):
                self.db.execute(
                    "UPDATE deadlines SET status=? WHERE id=?", (M.DL_BREACHED, r["id"]))
                breached.append(r["subject_ref"])
                self.audit("DEADLINE_BREACHED", "SYSTEM", {
                    "subject_type": r["subject_type"], "subject_ref": r["subject_ref"],
                    "due_at": r["due_at"],
                }, r["case_id"])
        self.db.commit()
        return {"breached": breached}

    # ------------------------------------------------------------------ #
    # 审计导出
    # ------------------------------------------------------------------ #
    def export_audit(self, case_ref: str | None = None) -> dict:
        """导出案件全貌 + 事件流（JSON 可序列化），用于上线核对与归档。"""
        if case_ref:
            case = self.get_case(case_ref)
            events = [row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM audit_events WHERE case_id=? ORDER BY id",
                (self._case_row(case_ref)["id"],))]
        else:
            case = None
            events = [row_to_dict(r) for r in self.db.query_all(
                "SELECT * FROM audit_events ORDER BY id")]
        return {
            "exported_at": self.now_iso(),
            "case": case,
            "events": events,
            "event_count": len(events),
        }
