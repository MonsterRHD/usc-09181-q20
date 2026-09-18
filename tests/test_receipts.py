"""外部回执：幂等去重、原始回执只读、带原因更正、跨日到达。"""
import unittest
from datetime import datetime, timezone

from service.domain import CorrectionStatus, ValidationError

from tests.helpers import ServiceTestCase, open_case


class ReceiptDedupTest(ServiceTestCase):
    def test_duplicate_receipt_notified_once(self):
        case = open_case(self.service)
        first = self.service.receive_receipt(
            case["id"], "ACQUIRER", "rcpt-001", {"code": "AR01", "text": "受理中"})
        self.assertFalse(first["duplicate"])
        # 相同回执重复到达（网络重试/对端重发）
        second = self.service.receive_receipt(
            case["id"], "ACQUIRER", "rcpt-001", {"code": "AR01", "text": "受理中"})
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])

        receipts = self.service.get_case_view(case["id"], role="auditor")["receipts"]
        self.assertEqual(len(receipts), 1)
        notices = [n for n in self.service.list_notifications(case["id"])
                   if n["kind"] == "RECEIPT_RECEIVED"]
        self.assertEqual(len(notices), 1, "重复回执不得重复通知")

    def test_different_keys_are_independent(self):
        case = open_case(self.service)
        self.service.receive_receipt(case["id"], "ACQUIRER", "k1", {"n": 1})
        self.service.receive_receipt(case["id"], "ACQUIRER", "k2", {"n": 2})
        notices = [n for n in self.service.list_notifications(case["id"])
                   if n["kind"] == "RECEIPT_RECEIVED"]
        self.assertEqual(len(notices), 2)


class ReceiptCorrectionTest(ServiceTestCase):
    def test_original_immutable_correction_with_reason(self):
        case = open_case(self.service)
        receipt = self.service.receive_receipt(
            case["id"], "BANK", "rcpt-100", {"amount_cents": 100_000, "currency": "USD"})

        correction = self.service.correct_receipt(
            receipt["id"], {"amount_cents": 99_000}, "银行回执金额录入有误，以清算文件为准",
            actor="agent-01")
        self.assertEqual(correction["status"], CorrectionStatus.SUBMITTED)
        self.assertEqual(correction["reason"], "银行回执金额录入有误，以清算文件为准")

        after = self.service.get_receipt(receipt["id"])
        self.assertEqual(after["payload"], {"amount_cents": 100_000, "currency": "USD"},
                         "原始回执不得被修改")
        self.assertEqual(len(after["corrections"]), 1)
        self.assertEqual(after["corrections"][0]["corrected_payload"], {"amount_cents": 99_000})

        actions = [a["action"] for a in self.service.audit_trail(case["id"])]
        self.assertIn("RECEIPT_CORRECTED", actions)

    def test_correction_requires_reason(self):
        case = open_case(self.service)
        receipt = self.service.receive_receipt(case["id"], "BANK", "rcpt-101", {"a": 1})
        with self.assertRaises(ValidationError):
            self.service.correct_receipt(receipt["id"], {"a": 2}, "", actor="agent-01")


class CrossDayReceiptTest(ServiceTestCase):
    def test_receipt_across_midnight_marks_local_dates(self):
        # 案件时区 Asia/Shanghai；时钟从 UTC 15:50（当地 23:50）跨过午夜
        self.clock.now = datetime(2026, 9, 18, 15, 50, tzinfo=timezone.utc)
        case = open_case(self.service, tz="Asia/Shanghai")
        req = self.service.create_evidence_request(
            case["id"], "补交易凭证", actor="agent-01",
            due_local="2026-09-19T18:00:00", due_timezone="Asia/Shanghai")

        day1 = self.service.receive_receipt(case["id"], "ACQUIRER", "day1", {"seq": 1})
        self.clock.advance(hours=1)  # UTC 16:50 -> 上海 9/19 00:50，跨日
        day2 = self.service.receive_receipt(case["id"], "ACQUIRER", "day2", {"seq": 2})

        self.assertEqual(day1["local_date"], "2026-09-18")
        self.assertEqual(day2["local_date"], "2026-09-19")

        # 截止为上海时间 9/19 18:00，跨日回执仍在时限内
        breached = self.service.check_deadlines()
        self.assertEqual(breached, [])
        self.service.submit_evidence(req["id"], "交易凭证", actor="agent-01")
        self.service.fulfill_evidence_request(req["id"], actor="agent-01")
        view = self.service.get_case_view(case["id"], role="auditor")
        self.assertEqual(view["deadlines"][0]["status"], "MET")

    def test_deadline_breach_marks_and_notifies(self):
        case = open_case(self.service, tz="Asia/Shanghai")
        # 截止：上海时间 9/19 02:00 = UTC 9/18 18:00（当前时钟 12:00 UTC，尚未到期）
        self.service.create_evidence_request(
            case["id"], "补交易凭证", actor="agent-01",
            due_local="2026-09-19T02:00:00", due_timezone="Asia/Shanghai")
        self.clock.advance(hours=7)  # UTC 19:00 -> 上海 9/19 03:00，已过截止
        breached = self.service.check_deadlines()
        self.assertEqual(len(breached), 1)
        view = self.service.get_case_view(case["id"], role="auditor")
        self.assertEqual(view["deadlines"][0]["status"], "BREACHED")
        self.assertEqual(view["evidence_requests"][0]["status"], "EXPIRED")
        kinds = [n["kind"] for n in self.service.list_notifications(case["id"])]
        self.assertIn("DEADLINE_BREACHED", kinds)
        # 再次扫描不重复通知
        self.service.check_deadlines()
        kinds = [n["kind"] for n in self.service.list_notifications(case["id"])]
        self.assertEqual(kinds.count("DEADLINE_BREACHED"), 1)


if __name__ == "__main__":
    unittest.main()
