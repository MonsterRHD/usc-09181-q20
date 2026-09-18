import os
import tempfile
import unittest

from service import models as M
from service.db import Database
from service.errors import DuplicateReceipt, ReceiptConflict
from service.workbench import Workbench
from tests.helpers import FakeClock, ScriptedGateway, case_payload


class ExternalRequestRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_pending_request_resurfaces_after_restart(self):
        clock = FakeClock()
        # 第一次运行：网关首次发送失败，请求留在 PENDING
        db1 = Database(self.path)
        gw1 = ScriptedGateway(fail_first=1)
        wb1 = Workbench(db1, gateway=gw1, clock=clock)
        case = wb1.create_case(case_payload(), "agent.li")
        wb1.request_evidence(case["case_ref"], M.PARTY_ACQUIRER, "授权记录", "agent.li")
        unfinished = wb1.list_unfinished_requests()
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(unfinished[0]["status"], M.REQ_PENDING)
        self.assertEqual(unfinished[0]["attempts"], 1)
        self.assertTrue(unfinished[0]["last_error"])
        db1.close()

        # 程序再次运行：同一数据库文件，未完成请求继续可追踪并重发
        db2 = Database(self.path)
        gw2 = ScriptedGateway(reconcile="ACKNOWLEDGED")
        wb2 = Workbench(db2, gateway=gw2, clock=clock)
        result = wb2.recover_external_requests()
        self.assertEqual(result["redispatched"], 1)
        # 再次恢复：DISPATCHED 与外部核对后得到 ACKNOWLEDGED
        result2 = wb2.recover_external_requests()
        self.assertEqual(result2["acknowledged"], 1)
        self.assertEqual(wb2.list_unfinished_requests(), [])
        # 整个生命周期只真正发出过一次（失败的那次不计入 sent）
        self.assertEqual(len(gw2.sent), 1)
        db2.close()

    def test_dispatched_without_receipt_stays_tracked(self):
        db1 = Database(self.path)
        gw = ScriptedGateway(reconcile="DISPATCHED")  # 外部仍未给回执
        wb = Workbench(db1, gateway=gw, clock=FakeClock())
        case = wb.create_case(case_payload(), "agent.li")
        wb.request_evidence(case["case_ref"], M.PARTY_MERCHANT, "签购单", "agent.li")
        db1.close()

        db2 = Database(self.path)
        wb2 = Workbench(db2, gateway=ScriptedGateway(reconcile="DISPATCHED"),
                        clock=FakeClock())
        wb2.recover_external_requests()
        unfinished = wb2.list_unfinished_requests()
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(unfinished[0]["status"], M.REQ_DISPATCHED)
        db2.close()


class ReceiptDedupTest(unittest.TestCase):
    def _case(self, wb):
        return wb.create_case(case_payload(), "agent.li")

    def test_duplicate_receipt_notifies_once(self):
        from tests.helpers import make_workbench
        wb, _ = make_workbench()
        case = self._case(wb)
        wb.receive_bank_receipt(case["case_ref"], "RCP-DUP-1", M.RECEIPT_REFUND,
                                '{"v":1}', "bank", currency="USD", amount_minor=4000)
        # REFUND 回执通知三个角色
        self.assertEqual(len(wb.list_notifications(M.ROLE_AGENT)), 1)
        self.assertEqual(len(wb.list_notifications(M.ROLE_SUPERVISOR)), 1)
        self.assertEqual(len(wb.list_notifications(M.ROLE_MERCHANT)), 1)

        # 相同回执重复到达：抛 DuplicateReceipt，不新增任何通知
        with self.assertRaises(DuplicateReceipt):
            wb.receive_bank_receipt(case["case_ref"], "RCP-DUP-1", M.RECEIPT_REFUND,
                                    '{"v":1}', "bank", currency="USD", amount_minor=4000)
        total = wb.db.query_one("SELECT COUNT(*) AS c FROM notifications")["c"]
        self.assertEqual(total, 3)
        # 重复事件仍留审计
        dup_events = wb.db.query_all(
            "SELECT COUNT(*) AS c FROM audit_events WHERE event_type='RECEIPT_DUPLICATE_IGNORED'")
        self.assertEqual(dup_events[0]["c"], 1)

    def test_same_uid_different_payload_is_conflict(self):
        from tests.helpers import make_workbench
        wb, _ = make_workbench()
        case = self._case(wb)
        wb.receive_bank_receipt(case["case_ref"], "RCP-X", M.RECEIPT_GENERIC, "payload-A",
                                "bank")
        with self.assertRaises(ReceiptConflict):
            wb.receive_bank_receipt(case["case_ref"], "RCP-X", M.RECEIPT_GENERIC,
                                    "payload-B", "bank")
        # 原始内容未被覆盖
        row = wb.db.query_one(
            "SELECT payload_text FROM bank_receipts WHERE receipt_uid=?", ("RCP-X",))
        self.assertEqual(row["payload_text"], "payload-A")

    def test_notification_body_is_masked_for_merchant(self):
        from tests.helpers import make_workbench
        wb, _ = make_workbench()
        case = self._case(wb)
        wb.receive_bank_receipt(case["case_ref"], "RCP-M-1", M.RECEIPT_REFUND,
                                '{"v":1}', "bank", currency="USD", amount_minor=4000)
        merchant_note = wb.list_notifications(M.ROLE_MERCHANT)[0]
        self.assertIn("Z**", merchant_note["body"])
        self.assertNotIn("Zhang Wei", merchant_note["body"])
        self.assertNotIn("zhang.wei@example.com", merchant_note["body"])
