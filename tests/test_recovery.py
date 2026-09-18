"""重启恢复：进程重启后未完成的外部请求继续可追踪。"""
import unittest

from service.case_service import CaseService
from service.db import Store

from tests.helpers import ServiceTestCase, open_case


class RecoveryTest(ServiceTestCase):
    def test_pending_external_requests_survive_restart(self):
        case = open_case(self.service)
        req = self.service.create_evidence_request(
            case["id"], "补签购单与物流凭证", actor="agent-01",
            due_local="2026-09-25T18:00:00", due_timezone="Asia/Shanghai")
        receipt = self.service.receive_receipt(
            case["id"], "ACQUIRER", "rcpt-restart-1", {"status": "CHARGEBACK_FILED"})
        correction = self.service.correct_receipt(
            receipt["id"], {"status": "CHARGEBACK_FILED", "arn": "240112"},
            "首次回执缺少 ARN 编号", actor="agent-01")

        # 模拟进程重启：同一数据库文件、全新的 service 实例（无内存状态）
        restarted = CaseService(Store(self.db_path), clock=self.clock)
        pending = restarted.pending_external_requests()

        kinds = {(p["type"], p.get("id")) for p in pending}
        self.assertIn(("EVIDENCE_REQUEST", req["id"]), kinds)
        self.assertIn(("RECEIPT_CORRECTION", correction["id"]), kinds)

        # 重启后案件状态、回执、审计链都还在
        view = restarted.get_case_view(case["id"], role="auditor")
        self.assertEqual(view["status"], "PENDING_EVIDENCE")
        self.assertEqual(view["receipts"][0]["idempotency_key"], "rcpt-restart-1")
        actions = [a["action"] for a in restarted.audit_trail(case["id"])]
        self.assertIn("RECEIPT_CORRECTED", actions)

        # 重启后补证照常进行，完成后该请求从待办中消失
        restarted.submit_evidence(req["id"], "签购单扫描件", actor="agent-01")
        restarted.fulfill_evidence_request(req["id"], actor="agent-01")
        remaining = [p for p in restarted.pending_external_requests()
                     if p["type"] == "EVIDENCE_REQUEST"]
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
