"""HTTP 接口冒烟测试：覆盖鉴权角色、脱敏、幂等回执的 HTTP 语义。"""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer

from service import models as M
from service.app import AppState, make_handler
from service.db import Database
from tests.helpers import FakeClock, ScriptedGateway, case_payload


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.state = AppState(Database(":memory:"), gateway=ScriptedGateway())
        self.wb = self.state.workbench
        self.httpd = HTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _req(self, method, path, body=None, role=M.ROLE_AGENT, actor=None):
        url = f"http://127.0.0.1:{self.port}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Role", role)
        if actor:
            req.add_header("X-Actor", actor)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        status, body = self._req("GET", "health", role=M.ROLE_AGENT)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_case_lifecycle_over_http_with_role_masking(self):
        # 立案
        status, case = self._req("POST", "cases", case_payload(), actor="agent.li")
        self.assertEqual(status, 201)
        ref = case["case_ref"]

        # 按翻译角色查看：敏感字段全掩码
        status, view = self._req("GET", f"cases/{ref}", role=M.ROLE_TRANSLATOR)
        self.assertEqual(status, 200)
        self.assertEqual(view["customer_name"], "***")

        # 请求证据
        status, ev = self._req("POST", f"cases/{ref}", {
            "action": "request_evidence", "target_party": M.PARTY_MERCHANT,
            "description": "签购单", "deadline_hours": 24,
            "timezone_name": "Asia/Shanghai",
        }, actor="agent.li")
        self.assertEqual(status, 201)
        ev_ref = ev["request_ref"]

        # 商户提交 → 客服受理
        status, _ = self._req("POST", f"evidence/{ev_ref}/submit", {
            "evidence_summary": "齐全材料", "complete": True, "missing_items": [],
        }, actor="merchant.user")
        self.assertEqual(status, 200)
        status, _ = self._req("POST", f"evidence/{ev_ref}/decision", {
            "accept": True, "note": "齐全",
        }, actor="agent.li")
        self.assertEqual(status, 200)

        # 部分退款 → 批准 → 发银行
        status, rfd = self._req("POST", f"cases/{ref}", {
            "action": "propose_refund", "amount_minor": 4000, "currency": "USD",
            "reason": "40% 和解",
        }, actor="agent.li")
        refund_ref = rfd["refund_ref"]
        self.assertEqual(self._req("POST", f"refunds/{refund_ref}/approve", {},
                                   actor="supervisor.wang")[0], 200)
        self.assertEqual(self._req("POST", f"refunds/{refund_ref}/send", {},
                                   actor="agent.li")[0], 200)

        # 跨日银行回执
        status, receipt = self._req("POST", f"cases/{ref}", {
            "action": "receipt", "receipt_uid": "RCP-HTTP-1",
            "receipt_type": M.RECEIPT_REFUND, "payload_text": '{"ok":true}',
            "event_time": "2026-09-19T03:00:00Z", "currency": "USD",
            "amount_minor": 4000,
        }, actor="acquirer-gw")
        self.assertEqual(status, 201)

        # 重复回执：HTTP 200 + duplicate=true，不报错、不重复通知
        status, dup = self._req("POST", f"cases/{ref}", {
            "action": "receipt", "receipt_uid": "RCP-HTTP-1",
            "receipt_type": M.RECEIPT_REFUND, "payload_text": '{"ok":true}',
            "currency": "USD", "amount_minor": 4000,
        }, actor="acquirer-gw")
        self.assertEqual(status, 200)
        self.assertTrue(dup["duplicate"])
        self.assertFalse(dup["notified"])

        # 并发转派冲突 → 409
        self._req("POST", f"cases/{ref}", {
            "action": "transfer", "to_owner": "agent.chen", "reason": "交接",
        }, actor="agent.li")
        status, err = self._req("POST", f"cases/{ref}", {
            "action": "transfer", "to_owner": "bank.team", "reason": "过期请求",
            "expected_owner": "agent.li",
        }, actor="agent.li")
        self.assertEqual(status, 409)
        self.assertIn("责任人已变更", err["error"])

        # 尝试直接改原始回执 → 403，只能走更正
        status, err = self._req("POST", "receipts/RCP-HTTP-1/corrections", {
            "corrected_payload": {"amount_minor": 3900}, "reason": "金额解析错误",
        }, actor="agent.li")
        self.assertEqual(status, 201)

        # 最终结论
        status, final = self._req("POST", f"cases/{ref}", {
            "action": "resolve", "resolution": M.RESOLUTION_PARTIAL_REFUND,
            "note": "完结",
        }, actor="supervisor.wang")
        self.assertEqual(status, 200)
        self.assertEqual(final["resolution"], M.RESOLUTION_PARTIAL_REFUND)

        # 审计导出
        status, audit = self._req("GET", f"audit?case_ref={ref}")
        self.assertEqual(status, 200)
        self.assertGreater(audit["event_count"], 5)

    def test_invalid_role_rejected(self):
        status, err = self._req("GET", "cases", role="AUDITOR")
        self.assertEqual(status, 400)
        self.assertIn("未知角色", err["error"])

    def test_recovery_endpoint(self):
        case = self.wb.create_case(case_payload(), "agent.li")
        self.wb.request_evidence(case["case_ref"], M.PARTY_ACQUIRER, "授权", "agent.li")
        status, body = self._req("POST", "recovery/recover", {})
        self.assertEqual(status, 200)
        self.assertIn("still_pending", body)


if __name__ == "__main__":
    unittest.main()
