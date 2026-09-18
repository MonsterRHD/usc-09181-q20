"""HTTP API 冒烟：建案、回执幂等、角色脱敏、错误格式。"""
import json
import threading
import unittest
import urllib.request
import urllib.error

from service.api import create_server

from tests.helpers import CUSTOMER, TXN, ServiceTestCase


class ApiTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.service, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, role="agent", actor="agent-01"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "X-Role": role, "X-Actor": actor})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body), (200, {"status": "ok"}))

    def test_case_flow_over_http(self):
        status, case = self.call("POST", "/cases", {
            "transaction": TXN, "customer": CUSTOMER,
            "timezone": "Asia/Shanghai", "assignee": "agent-01"})
        self.assertEqual(status, 201)
        case_id = case["id"]

        # 回执幂等：第一次 201，重复到达 200 且通知不增加
        receipt = {"source": "ACQUIRER", "idempotency_key": "k-1", "payload": {"s": 1}}
        status, _ = self.call("POST", f"/cases/{case_id}/receipts", receipt)
        self.assertEqual(status, 201)
        status, dup = self.call("POST", f"/cases/{case_id}/receipts", receipt)
        self.assertEqual(status, 200)
        self.assertTrue(dup["duplicate"])
        _, notices = self.call("GET", f"/cases/{case_id}/notifications")
        self.assertEqual(len([n for n in notices if n["kind"] == "RECEIPT_RECEIVED"]), 1)

        # 角色脱敏：商户视图看不到客户邮箱
        _, merchant_view = self.call("GET", f"/cases/{case_id}", role="merchant")
        self.assertEqual(merchant_view["customer_email"], "***")
        _, auditor_view = self.call("GET", f"/cases/{case_id}", role="auditor")
        self.assertEqual(auditor_view["customer_email"], CUSTOMER["email"])

        # 乐观锁：错误版本转派返回 409 与稳定错误码
        status, err = self.call("POST", f"/cases/{case_id}/transfer",
                                {"to": "agent-02", "reason": "转派", "expected_version": 99})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "CONFLICT")

        # 审计导出
        _, audit = self.call("GET", f"/cases/{case_id}/audit", role="auditor")
        self.assertIn("CASE_CREATED", [a["action"] for a in audit])

    def test_unknown_route_and_bad_json(self):
        status, err = self.call("GET", "/nope")
        self.assertEqual(status, 404)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/cases", method="POST",
            data=b"{not json", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
