"""案件主流程：补证、部分退款、翻译版本、重复申诉、客户撤回。"""
import unittest

from service.domain import (
    AppealStatus,
    CaseStatus,
    ConflictError,
    EvidenceRequestStatus,
    RefundStatus,
    TranslationStatus,
    ValidationError,
)

from tests.helpers import ServiceTestCase, open_case


class EvidenceFlowTest(ServiceTestCase):
    def test_evidence_request_submit_fulfill(self):
        case = open_case(self.service)
        self.assertEqual(case["status"], CaseStatus.OPEN)

        req = self.service.create_evidence_request(
            case["id"], "请提供签购单与商户沟通记录", actor="agent-01")
        self.assertEqual(req["status"], EvidenceRequestStatus.OPEN)
        self.assertEqual(self.service.get_case(case["id"])["status"], CaseStatus.PENDING_EVIDENCE)

        # 补证：先补一份 -> 部分完成；再补一份 -> 仍部分完成；确认齐全 -> FULFILLED
        r1 = self.service.submit_evidence(req["id"], "签购单扫描件", actor="agent-01")
        self.assertEqual(r1["status"], EvidenceRequestStatus.PARTIAL)
        self.service.submit_evidence(req["id"], "与商户的邮件往来", actor="agent-01")
        req = self.service.fulfill_evidence_request(req["id"], actor="agent-01")
        self.assertEqual(req["status"], EvidenceRequestStatus.FULFILLED)
        self.assertEqual(len(req["items"]), 2)
        # 所有证据请求完成后回到 UNDER_REVIEW
        self.assertEqual(self.service.get_case(case["id"])["status"], CaseStatus.UNDER_REVIEW)

    def test_fulfill_without_items_rejected(self):
        case = open_case(self.service)
        req = self.service.create_evidence_request(case["id"], "补签购单", actor="agent-01")
        with self.assertRaises(ConflictError):
            self.service.fulfill_evidence_request(req["id"], actor="agent-01")

    def test_resolve_blocked_while_evidence_open(self):
        case = open_case(self.service)
        self.service.create_evidence_request(case["id"], "补签购单", actor="agent-01")
        with self.assertRaises(ConflictError):
            self.service.resolve_case(case["id"], "REJECTED", "材料未齐", actor="agent-01")


class RefundFlowTest(ServiceTestCase):
    def test_partial_refund_lifecycle(self):
        case = open_case(self.service)
        refund = self.service.propose_refund(case["id"], 30_000, "商户同意部分退款", actor="agent-01")
        self.assertEqual(refund["status"], RefundStatus.PROPOSED)
        refund = self.service.approve_refund(refund["id"], actor="supervisor-01")
        self.assertEqual(refund["status"], RefundStatus.APPROVED)
        refund = self.service.settle_refund(refund["id"], actor="system")
        self.assertEqual(refund["status"], RefundStatus.SETTLED)

    def test_refund_cannot_exceed_transaction(self):
        case = open_case(self.service)
        self.service.propose_refund(case["id"], 80_000, "第一笔", actor="agent-01")
        with self.assertRaises(ValidationError):
            self.service.propose_refund(case["id"], 30_000, "超额", actor="agent-01")

    def test_refund_state_machine(self):
        case = open_case(self.service)
        refund = self.service.propose_refund(case["id"], 10_000, "测试", actor="agent-01")
        with self.assertRaises(ConflictError):
            self.service.settle_refund(refund["id"], actor="system")  # 未批准不能结算


class TranslationTest(ServiceTestCase):
    def test_versions_supersede(self):
        case = open_case(self.service)
        v1 = self.service.add_translation(case["id"], "receipt-1", "zh", "初译", actor="translator-01")
        v2 = self.service.add_translation(case["id"], "receipt-1", "zh", "修订译", actor="translator-02")
        self.assertEqual((v1["version"], v2["version"]), (1, 2))
        self.assertEqual(self.service.get_translation(v1["id"])["status"], TranslationStatus.SUPERSEDED)
        self.assertEqual(v2["status"], TranslationStatus.DRAFT)
        v2 = self.service.approve_translation(v2["id"], actor="supervisor-01")
        self.assertEqual(v2["status"], TranslationStatus.APPROVED)
        # 不同语言互不影响
        en = self.service.add_translation(case["id"], "receipt-1", "en", "EN version", actor="translator-01")
        self.assertEqual(en["version"], 1)


class AppealTest(ServiceTestCase):
    def _resolved_case(self):
        case = open_case(self.service)
        self.service.resolve_case(case["id"], "REJECTED", "证据不足", actor="agent-01")
        return case

    def test_duplicate_appeal_rejected(self):
        case = self._resolved_case()
        appeal = self.service.submit_appeal(case["id"], "新证据", actor="agent-01")
        self.assertEqual(appeal["status"], AppealStatus.SUBMITTED)
        with self.assertRaises(ConflictError):
            self.service.submit_appeal(case["id"], "新证据", actor="agent-01")

    def test_accept_appeal_reopens_case(self):
        case = self._resolved_case()
        appeal = self.service.submit_appeal(case["id"], "新证据", actor="agent-01")
        self.service.accept_appeal(appeal["id"], actor="supervisor-01")
        reopened = self.service.get_case(case["id"])
        self.assertEqual(reopened["status"], CaseStatus.UNDER_REVIEW)
        self.assertIsNone(reopened["resolution"])

    def test_appeal_on_open_case_rejected(self):
        case = open_case(self.service)
        with self.assertRaises(ConflictError):
            self.service.submit_appeal(case["id"], "太早了", actor="agent-01")


class WithdrawTest(ServiceTestCase):
    def test_withdraw_cancels_pending_work(self):
        case = open_case(self.service)
        req = self.service.create_evidence_request(
            case["id"], "补签购单", actor="agent-01",
            due_local="2026-09-20T18:00:00", due_timezone="Asia/Shanghai")
        self.service.withdraw_case(case["id"], "客户与商户私下和解", actor="agent-01")
        view = self.service.get_case_view(case["id"], role="auditor")
        self.assertEqual(view["status"], CaseStatus.WITHDRAWN)
        self.assertEqual(view["evidence_requests"][0]["status"], EvidenceRequestStatus.CANCELLED)
        self.assertEqual(view["deadlines"][0]["status"], "CANCELLED")
        # 终态后不能再发证据请求
        with self.assertRaises(ConflictError):
            self.service.create_evidence_request(case["id"], "再补一份", actor="agent-01")

    def test_withdraw_requires_reason(self):
        case = open_case(self.service)
        with self.assertRaises(ValidationError):
            self.service.withdraw_case(case["id"], "", actor="agent-01")


if __name__ == "__main__":
    unittest.main()
