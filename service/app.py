"""HTTP 适配层（标准库 http.server）。

角色通过 X-Role 请求头传递（默认 AGENT），所有返回数据按角色脱敏。
错误码映射：校验 400 / 找不到 404 / 状态非法 409 / 并发冲突 409 /
重复回执 200(duplicate=true) / 回执内容冲突 409 / 不可变回执 403。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from . import models as M
from .db import Database
from .errors import (
    DuplicateReceipt,
    IllegalTransition,
    ImmutableReceipt,
    NotFound,
    OwnerChanged,
    ReceiptConflict,
    ValidationError,
    WorkbenchError,
)
from .workbench import ExternalGateway, Workbench

CONFLICT_ERRORS = (IllegalTransition, OwnerChanged, ReceiptConflict)


class AppState:
    def __init__(self, db: Database | None = None, gateway: ExternalGateway | None = None):
        self.db = db or Database()
        self.workbench = Workbench(self.db, gateway=gateway)


def make_handler(state: AppState):
    wb = state.workbench

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        # ------------------------------------------------------------ #
        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise ValidationError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        def _role(self) -> str:
            role = self.headers.get("X-Role", M.ROLE_AGENT).upper()
            if role not in M.ALL_ROLES:
                raise ValidationError(f"未知角色: {role}")
            return role

        def _actor(self, body: dict) -> str:
            return body.pop("actor", None) or self.headers.get("X-Actor") or self._role()

        def _handle_error(self, exc: Exception):
            if isinstance(exc, DuplicateReceipt):
                self._send(200, {"duplicate": True, "notified": False, "message": str(exc)})
            elif isinstance(exc, ValidationError):
                self._send(400, {"error": str(exc)})
            elif isinstance(exc, NotFound):
                self._send(404, {"error": str(exc)})
            elif isinstance(exc, ImmutableReceipt):
                self._send(403, {"error": str(exc)})
            elif isinstance(exc, CONFLICT_ERRORS):
                self._send(409, {"error": str(exc)})
            elif isinstance(exc, WorkbenchError):
                self._send(422, {"error": str(exc)})
            else:
                raise exc

        # ------------------------------------------------------------ #
        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path.strip("/")
                parts = path.split("/") if path else []
                role = self._role()

                if path == "health":
                    self._send(200, {"status": "ok"})
                elif path == "cases":
                    self._send(200, {"cases": wb.list_cases(role)})
                elif len(parts) == 2 and parts[0] == "cases":
                    self._send(200, wb.get_case(parts[1], role))
                elif path == "notifications":
                    self._send(200, {"notifications": wb.list_notifications(role)})
                elif path == "requests":
                    self._send(200, {"unfinished": wb.list_unfinished_requests()})
                elif path == "audit":
                    qs = parse_qs(parsed.query)
                    self._send(200, wb.export_audit(qs.get("case_ref", [None])[0]))
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                path = parsed.path.strip("/")
                parts = path.split("/") if path else []
                body = self._read_json()
                role = self._role()
                actor = self._actor(body)

                # ---- 案件 ----
                if path == "cases":
                    self._send(201, wb.create_case(body, actor))
                elif len(parts) == 2 and parts[0] == "cases" and parts[1] != "":
                    case_ref = parts[1]
                    action = body.pop("action", None)
                    self._route_case_action(case_ref, action, body, actor, role)
                elif len(parts) == 3 and parts[0] == "evidence":
                    self._route_evidence(parts[1], parts[2], body, actor)
                elif len(parts) == 3 and parts[0] == "refunds":
                    self._route_refund(parts[1], parts[2], body, actor)
                elif len(parts) == 5 and parts[0] == "cases" and parts[2] == "translations":
                    if parts[4] == "delivery":
                        result = wb.deliver_translation(
                            parts[1], int(parts[3]), body["translated_text"],
                            body.get("translator", actor), actor)
                        self._send(200, result)
                elif len(parts) == 3 and parts[0] == "receipts" and parts[2] == "corrections":
                    result = wb.correct_receipt(
                        parts[1], body["corrected_payload"], body["reason"], actor)
                    self._send(201, result)
                elif len(parts) == 3 and parts[0] == "withdrawals" and parts[2] == "confirm":
                    result = wb.confirm_withdrawal(parts[1], actor)
                    self._send(200, result)
                elif len(parts) == 3 and parts[0] == "authorizations" and parts[2] == "revoke":
                    result = wb.revoke_authorization(parts[1], actor)
                    self._send(200, result)
                elif path == "recovery/recover":
                    self._send(200, wb.recover_external_requests())
                elif path == "deadlines/sweep":
                    self._send(200, wb.sweep_deadlines())
                elif path == "notifications/deliver":
                    self._send(200, {"delivered": wb.deliver_pending_notifications()})
                else:
                    self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                self._handle_error(exc)

        # ------------------------------------------------------------ #
        def _route_case_action(self, case_ref, action, body, actor, role):
            if action == "transfer":
                result = wb.transfer_case(
                    case_ref, body["to_owner"], body["reason"], actor,
                    expected_owner=body.get("expected_owner"),
                    escalate=bool(body.get("escalate")))
                self._send(200, result)
            elif action == "authorization":
                result = wb.record_authorization(
                    case_ref, body["kind"], bool(body["granted"]), actor,
                    auth_ref=body.get("auth_ref"),
                    window_hours=body.get("window_hours"),
                    snapshot=body.get("snapshot"))
                self._send(201, result)
            elif action == "request_evidence":
                result = wb.request_evidence(
                    case_ref, body["target_party"], body["description"], actor,
                    deadline_hours=float(body.get("deadline_hours", 72)),
                    timezone_name=body.get("timezone_name", "UTC"),
                    request_ref=body.get("request_ref"))
                self._send(201, result)
            elif action == "propose_refund":
                result = wb.propose_refund(
                    case_ref, int(body["amount_minor"]), body["currency"],
                    body.get("reason", ""), actor, kind=body.get("kind", M.REFUND_KIND_PARTIAL))
                self._send(201, result)
            elif action == "request_translation":
                result = wb.request_translation(
                    case_ref, body["source_lang"], body["target_lang"],
                    body["source_text"], actor)
                self._send(201, result)
            elif action == "withdraw":
                result = wb.request_withdrawal(case_ref, body.get("reason", ""), actor)
                self._send(201, result)
            elif action == "resolve":
                result = wb.resolve_case(
                    case_ref, body["resolution"], actor, body.get("note", ""))
                self._send(200, result)
            elif action == "receipt":
                result = wb.receive_bank_receipt(
                    case_ref, body["receipt_uid"], body["receipt_type"],
                    body["payload_text"], actor,
                    event_time=body.get("event_time"),
                    currency=body.get("currency"),
                    amount_minor=(int(body["amount_minor"])
                                  if body.get("amount_minor") is not None else None),
                    channel=body.get("channel", "WORKBENCH"))
                self._send(201, result)
            else:
                self._send(400, {"error": f"未知或缺少 action: {action!r}"})

        def _route_evidence(self, request_ref, action, body, actor):
            if action == "submit":
                result = wb.submit_evidence(
                    request_ref, body["evidence_summary"], bool(body.get("complete", False)),
                    body.get("missing_items", []), actor)
            elif action == "decision":
                result = wb.decide_evidence(
                    request_ref, bool(body["accept"]), body.get("note", ""), actor)
            else:
                self._send(400, {"error": f"未知 action: {action}"})
                return
            self._send(200, result)

        def _route_refund(self, refund_ref, action, body, actor):
            if action == "approve":
                result = wb.approve_refund(refund_ref, actor)
            elif action == "send":
                result = wb.send_refund_to_bank(refund_ref, actor)
            else:
                self._send(400, {"error": f"未知 action: {action}"})
                return
            self._send(200, result)

    return Handler


def serve(state: AppState | None = None, host: str = "0.0.0.0", port: int | None = None) -> HTTPServer:
    import os
    state = state or AppState()
    port = port or int(os.getenv("PORT", "8000"))
    httpd = HTTPServer((host, port), make_handler(state))
    return httpd
