import unittest

from service import models as M
from service.errors import IllegalTransition, NotFound, ValidationError
from tests.helpers import FakeClock, ScriptedGateway, case_payload, make_workbench


class EvidenceTest(unittest.TestCase):
    def test_request_submit_decision_and_second_request_after_rejection(self):
        wb, clock = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        self.assertEqual(case["status"], M.CASE_OPEN)

        req = wb.request_evidence(case["case_ref"], M.PARTY_MERCHANT, "请提供签购单",
                                  "agent.li", deadline_hours=72,
                                  timezone_name="America/New_York")
        self.assertEqual(req["status"], M.EV_REQUESTED)
        self.assertIn("America/New_York", req["deadline_label"])
        case = wb.get_case(case["case_ref"])
        self.assertEqual(case["status"], M.CASE_EVIDENCE_REQUESTED)

        # 材料不齐：先补交
        wb.submit_evidence(req["request_ref"], "收据照片", False, ["客户授权书"], "merchant")
        # 受理裁决：证据不足，驳回 → 需要重新补证
        wb.decide_evidence(req["request_ref"], False, "缺少客户授权书", "agent.li")
        req2 = wb.request_evidence(case["case_ref"], M.PARTY_MERCHANT, "补充客户授权书",
                                   "agent.li", deadline_hours=24)
        wb.submit_evidence(req2["request_ref"], "授权书+收据", True, [], "merchant")
        wb.decide_evidence(req2["request_ref"], True, "材料齐全", "agent.li")
        case = wb.get_case(case["case_ref"])
        self.assertEqual(case["status"], M.CASE_IN_REVIEW)
        # 旧请求保留为 REJECTED，不被覆盖
        statuses = {r["request_ref"]: r["status"] for r in case["evidence_requests"]}
        self.assertEqual(statuses[req["request_ref"]], M.EV_REJECTED)
        self.assertEqual(statuses[req2["request_ref"]], M.EV_ACCEPTED)

    def test_overdue_evidence_marks_deadline_breached(self):
        gateway = ScriptedGateway()
        clock = FakeClock()
        wb, clock = make_workbench(gateway=gateway, clock=clock)
        case = wb.create_case(case_payload(), "agent.li")
        req = wb.request_evidence(case["case_ref"], M.PARTY_ACQUIRER, "授权记录",
                                  "agent.li", deadline_hours=48,
                                  timezone_name="Asia/Shanghai")
        # 未到时限：不超期
        self.assertEqual(wb.sweep_deadlines()["breached"], [])
        clock.advance(hours=49)  # 跨时区/跨日后
        result = wb.sweep_deadlines()
        self.assertIn(req["request_ref"], result["breached"])
        case = wb.get_case(case["case_ref"])
        dl = [d for d in case["deadlines"] if d["subject_ref"] == req["request_ref"]][0]
        self.assertEqual(dl["status"], M.DL_BREACHED)


class RefundTest(unittest.TestCase):
    def _approved_sent_refund(self, wb, case, amount=4000):
        rfd = wb.propose_refund(case["case_ref"], amount, "USD",
                                "商户同意承担部分金额", "agent.li")
        wb.approve_refund(rfd["refund_ref"], "supervisor.wang")
        wb.send_refund_to_bank(rfd["refund_ref"], "agent.li")
        return wb.db.query_one("SELECT * FROM refunds WHERE refund_ref=?",
                               (rfd["refund_ref"],))

    def test_partial_refund_flow_and_amount_guard(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        with self.assertRaises(ValidationError):
            wb.propose_refund(case["case_ref"], 20000, "USD", "超额", "agent.li")
        rfd = self._approved_sent_refund(wb, case)
        self.assertEqual(rfd["status"], M.RF_SENT_TO_BANK)
        # 银行退款回执（跨日事件时间）确认部分退款
        receipt = wb.receive_bank_receipt(
            case["case_ref"], "RCP-RFD-1", M.RECEIPT_REFUND,
            '{"request_uid":"X"}', "bank",
            event_time="2026-09-19T02:05:00Z", currency="USD", amount_minor=4000)
        self.assertEqual(receipt["receipt_type"], M.RECEIPT_REFUND)
        case = wb.get_case(case["case_ref"])
        refund = [r for r in case["refunds"] if r["refund_ref"] == rfd["refund_ref"]][0]
        self.assertEqual(refund["status"], M.RF_CONFIRMED)
        self.assertEqual(refund["linked_receipt_uid"], "RCP-RFD-1")

    def test_resolve_requires_confirmed_refund_for_partial_resolution(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        with self.assertRaises(IllegalTransition):
            wb.resolve_case(case["case_ref"], M.RESOLUTION_PARTIAL_REFUND,
                            "supervisor.wang", "缺银行确认")


class TranslationTest(unittest.TestCase):
    def test_versions_are_append_only_and_old_superseded(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        v1 = wb.request_translation(case["case_ref"], "zh", "en", "客户声明原文一",
                                    "agent.li")
        self.assertEqual(v1["version"], 1)
        wb.deliver_translation(case["case_ref"], 1, "customer statement v1",
                               "translator.ay", "translator.ay")
        v2 = wb.request_translation(case["case_ref"], "zh", "en", "客户声明原文二",
                                    "agent.li")
        wb.deliver_translation(case["case_ref"], 2, "customer statement v2",
                               "translator.ay", "translator.ay")
        case = wb.get_case(case["case_ref"])
        versions = {t["version"]: t["status"] for t in case["translations"]}
        self.assertEqual(versions[1], M.TR_SUPERSEDED)
        self.assertEqual(versions[2], M.TR_DELIVERED)
        # v1 译文内容仍可查，不被覆盖
        v1row = [t for t in case["translations"] if t["version"] == 1][0]
        self.assertEqual(v1row["translated_text"], "customer statement v1")


class WithdrawalTest(unittest.TestCase):
    def test_customer_withdraws_and_case_closes(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        wd = wb.request_withdrawal(case["case_ref"], "客户来电表示不再追究", "agent.li")
        # 重复撤回请求被拒绝，状态单一
        with self.assertRaises(IllegalTransition):
            wb.request_withdrawal(case["case_ref"], "再次撤回", "agent.li")
        wb.confirm_withdrawal(wd["withdrawal_ref"], "supervisor.wang")
        case = wb.get_case(case["case_ref"])
        self.assertEqual(case["status"], M.CASE_RESOLVED)
        self.assertEqual(case["resolution"], M.RESOLUTION_WITHDRAWN)
        # 撤回后不能再转派
        from service.errors import IllegalTransition as IT
        with self.assertRaises(IT):
            wb.transfer_case(case["case_ref"], "agent.chen", "终结后转派", "agent.li")


class DuplicateAppealTest(unittest.TestCase):
    def test_same_transaction_appeal_is_linked_not_reopened(self):
        wb, _ = make_workbench()
        first = wb.create_case(case_payload(), "agent.li")
        second = wb.create_case(case_payload(), "agent.chen")
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["duplicate_of"], first["case_ref"])
        # 不同争议码可以另立案
        other = wb.create_case(case_payload(reason_code="13.1"), "agent.chen")
        self.assertFalse(other["duplicate"])


class ResponsibilityChainTest(unittest.TestCase):
    def test_transfer_and_escalation_leave_ordered_chain(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        wb.transfer_case(case["case_ref"], "agent.chen", "需要西语支持", "agent.li")
        wb.transfer_case(case["case_ref"], "supervisor.wang", "超权限升级",
                         "agent.chen", escalate=True)
        view = wb.get_case(case["case_ref"])
        self.assertEqual(view["status"], M.CASE_ESCALATED)
        chain = view["responsibility_chain"]
        self.assertEqual([h["to_owner"] for h in chain],
                         ["agent.li", "agent.chen", "supervisor.wang"])
        self.assertEqual([h["kind"] for h in chain[1:]],
                         [M.HANDOVER_TRANSFER, M.HANDOVER_ESCALATION])

    def test_concurrent_transfer_optimistic_lock(self):
        from service.errors import OwnerChanged
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        # 客服 A 以为案件还在自己手上，但 B 已转走
        wb.transfer_case(case["case_ref"], "agent.chen", "B 先处理", "agent.li")
        with self.assertRaises(OwnerChanged):
            wb.transfer_case(case["case_ref"], "bank.team", "A 的过时请求",
                             "agent.li", expected_owner="agent.li")
        # 不带乐观锁则正常转派，链上仍有完整记录
        wb.transfer_case(case["case_ref"], "bank.team", "按当前责任人继续转派",
                         "agent.chen")
        self.assertEqual(wb.get_case(case["case_ref"])["current_owner"], "bank.team")
