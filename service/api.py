"""HTTP API：把 CaseService 暴露为 JSON 接口。

身份通过请求头传入：X-Actor（操作人）、X-Role（角色，用于脱敏）。
路由保持 REST 风格，所有错误统一为 {"error": {...}}。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .case_service import CaseService
from .domain import DomainError


def make_handler(service: CaseService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------------------------------------------------------- 基础设施
        def log_message(self, *_):
            pass

        def _send(self, status: int, body: dict | list):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, exc: DomainError):
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode())
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON", 400, "BAD_JSON")

        @property
        def _actor(self) -> str:
            return self.headers.get("X-Actor", "anonymous")

        @property
        def _role(self) -> str:
            return self.headers.get("X-Role", "")

        # ---------------------------------------------------------- 路由
        def do_GET(self):
            try:
                self._route("GET")
            except DomainError as exc:
                self._error(exc)
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        def do_POST(self):
            try:
                self._route("POST")
            except DomainError as exc:
                self._error(exc)
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

        def _route(self, method: str):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            body = self._body() if method == "POST" else {}

            if method == "GET" and path == "/health":
                return self._send(200, {"status": "ok"})

            routes = [
                ("POST", r"^/cases$",
                 lambda m: self._send(201, service.create_case(
                     transaction=body.get("transaction", {}),
                     customer=body.get("customer", {}),
                     tz=body.get("timezone", "UTC"),
                     actor=self._actor,
                     assignee=body.get("assignee")))),
                ("GET", r"^/cases/(?P<id>[\w-]+)$",
                 lambda m: self._send(200, service.get_case_view(m["id"], role=self._role))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/authorization$",
                 lambda m: self._send(200, service.set_authorization(
                     m["id"], granted=bool(body.get("granted")), channel=body.get("channel", ""),
                     actor=self._actor, reason=body.get("reason")))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/evidence-requests$",
                 lambda m: self._send(201, service.create_evidence_request(
                     m["id"], description=body.get("description", ""), actor=self._actor,
                     due_local=body.get("due_local"), due_timezone=body.get("due_timezone")))),
                ("POST", r"^/evidence-requests/(?P<id>[\w-]+)/submit$",
                 lambda m: self._send(200, service.submit_evidence(
                     m["id"], content=body.get("content", ""), actor=self._actor))),
                ("POST", r"^/evidence-requests/(?P<id>[\w-]+)/fulfill$",
                 lambda m: self._send(200, service.fulfill_evidence_request(m["id"], actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/receipts$",
                 lambda m: self._post_receipt(m["id"], body)),
                ("POST", r"^/receipts/(?P<id>[\w-]+)/corrections$",
                 lambda m: self._send(201, service.correct_receipt(
                     m["id"], corrected_payload=body.get("corrected_payload", {}),
                     reason=body.get("reason", ""), actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/refunds$",
                 lambda m: self._send(201, service.propose_refund(
                     m["id"], amount_cents=body.get("amount_cents"), reason=body.get("reason", ""),
                     actor=self._actor))),
                ("POST", r"^/refunds/(?P<id>[\w-]+)/(?P<action>approve|settle|reject)$",
                 lambda m: self._send(200, getattr(service, f"{m['action']}_refund")(
                     m["id"], actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/appeals$",
                 lambda m: self._send(201, service.submit_appeal(
                     m["id"], ground=body.get("ground", ""), actor=self._actor))),
                ("POST", r"^/appeals/(?P<id>[\w-]+)/(?P<action>accept|reject)$",
                 lambda m: self._send(200, getattr(service, f"{m['action']}_appeal")(
                     m["id"], actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/translations$",
                 lambda m: self._send(201, service.add_translation(
                     m["id"], document_ref=body.get("document_ref", ""),
                     language=body.get("language", ""), content=body.get("content", ""),
                     actor=self._actor))),
                ("POST", r"^/translations/(?P<id>[\w-]+)/approve$",
                 lambda m: self._send(200, service.approve_translation(m["id"], actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/withdraw$",
                 lambda m: self._send(200, service.withdraw_case(
                     m["id"], reason=body.get("reason", ""), actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/transfer$",
                 lambda m: self._send(200, service.transfer_case(
                     m["id"], to_assignee=body.get("to", ""), reason=body.get("reason", ""),
                     actor=self._actor, expected_version=int(body.get("expected_version", 0))))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/escalate$",
                 lambda m: self._send(200, service.escalate_case(
                     m["id"], to_team=body.get("to", ""), reason=body.get("reason", ""),
                     actor=self._actor, expected_version=int(body.get("expected_version", 0))))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/resolve$",
                 lambda m: self._send(200, service.resolve_case(
                     m["id"], resolution=body.get("resolution", ""), note=body.get("note", ""),
                     actor=self._actor))),
                ("POST", r"^/cases/(?P<id>[\w-]+)/close$",
                 lambda m: self._send(200, service.close_case(m["id"], actor=self._actor))),
                ("POST", r"^/deadlines/check$",
                 lambda m: self._send(200, {"breached": service.check_deadlines()})),
                ("GET", r"^/cases/(?P<id>[\w-]+)/notifications$",
                 lambda m: self._send(200, service.list_notifications(m["id"]))),
                ("GET", r"^/cases/(?P<id>[\w-]+)/audit$",
                 lambda m: self._send(200, service.audit_trail(m["id"]))),
                ("GET", r"^/external-requests/pending$",
                 lambda m: self._send(200, service.pending_external_requests())),
            ]
            for verb, pattern, handler in routes:
                if verb != method:
                    continue
                match = re.match(pattern, path)
                if match:
                    return handler(match.groupdict())
            self._send(404, {"error": {"code": "NOT_FOUND", "message": f"无此路由: {method} {path}"}})

        def _post_receipt(self, case_id: str, body: dict):
            receipt = service.receive_receipt(
                case_id, source=body.get("source", ""),
                idempotency_key=body.get("idempotency_key", ""),
                payload=body.get("payload", {}), actor=self._actor)
            # 重复到达的回执返回 200 与首次记录，不重复通知
            self._send(200 if receipt.get("duplicate") else 201, receipt)

    return Handler


def create_server(service: CaseService, host: str = "0.0.0.0", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service))
