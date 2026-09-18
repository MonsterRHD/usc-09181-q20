"""上线前端到端演练：一笔交易完整经历
补证 → 部分退款 → 跨日回执 → 并发转派，
并核对最终结论、通知去重与审计导出，以及重启后外部请求可追踪。
"""
import json
import os
import tempfile
import unittest

from service import models as M
from service.db import Database
from service.errors import DuplicateReceipt, OwnerChanged
from service.timeutil import day_bucket_utc
from service.workbench import Workbench
from tests.helpers import FakeClock, ScriptedGateway, case_payload


class EndToEndDrillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        self.clock = FakeClock()  # 2026-09-18 10:00 UTC
        self.gateway = ScriptedGateway()
        self.db = Database(self.path)
        self.wb = Workbench(self.db, gateway=self.gateway, clock=self.clock)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_full_drill(self):
        wb, clock = self.wb, self.clock

        # 1) 立案：美国商户、美元交易
        case = wb.create_case(case_payload(), "agent.li")
        ref = case["case_ref"]
        self.assertEqual(case["current_owner"], "agent.li")

        # 2) 补证：首次材料不全 → 驳回 → 二次补证齐全
        ev1 = wb.request_evidence(ref, M.PARTY_MERCHANT, "签购单与身份核验记录",
                                  "agent.li", deadline_hours=48,
                                  timezone_name="America/New_York")
        wb.submit_evidence(ev1["request_ref"], "仅签购单照片", False,
                           ["客户身份核验记录"], "merchant")
        wb.decide_evidence(ev1["request_ref"], False, "缺身份核验记录", "agent.li")
        ev2 = wb.request_evidence(ref, M.PARTY_MERCHANT, "补充客户身份核验记录",
                                  "agent.li", deadline_hours=24,
                                  timezone_name="America/New_York")
        wb.submit_evidence(ev2["request_ref"], "签购单+护照核验记录", True, [],
                           "merchant")
        wb.decide_evidence(ev2["request_ref"], True, "材料齐全", "agent.li")
        self.assertEqual(wb.get_case(ref)["status"], M.CASE_IN_REVIEW)

        # 3) 部分退款：提出 → 主管批准 → 发送收单行（在途外部请求）
        rfd = wb.propose_refund(ref, 4000, "USD", "商户同意承担 40%", "agent.li")
        wb.approve_refund(rfd["refund_ref"], "supervisor.wang")
        wb.send_refund_to_bank(rfd["refund_ref"], "agent.li")
        refund_req = [r for r in wb.list_unfinished_requests()
                      if r["request_type"] == "REFUND"][0]
        self.assertEqual(refund_req["status"], M.REQ_DISPATCHED)

        # 4) 并发转派：B 已先转走案件，A 基于过期责任人的转派失败；
        #    按当前责任人继续转派并升级，责任链完整留痕
        wb.transfer_case(ref, "agent.chen", "夜班接手跨时区跟进", "agent.li")
        with self.assertRaises(OwnerChanged):
            wb.transfer_case(ref, "translator.team", "需要笔译支持", "agent.li",
                             expected_owner="agent.li")
        wb.transfer_case(ref, "supervisor.wang", "退款金额超一线权限",
                         "agent.chen", escalate=True)
        self.assertEqual(wb.get_case(ref)["status"], M.CASE_ESCALATED)

        # 5) 跨日回执：时钟推进到次日（UTC），银行 REFUND 回执到达
        clock.advance(hours=16)  # 2026-09-19 02:00 UTC
        receipt_payload = json.dumps({"request_uid": refund_req["request_uid"],
                                      "settled": True})
        receipt = wb.receive_bank_receipt(
            ref, "RCP-20260919-0007", M.RECEIPT_REFUND, receipt_payload, "acquirer-gw",
            event_time=wb.now_iso(), currency="USD", amount_minor=4000)
        # 事件 UTC 日期晚于立案日 → 跨日回执被正确归档而不误判/重复
        self.assertNotEqual(day_bucket_utc(receipt["event_time"]),
                            day_bucket_utc(case["created_at"]))
        case_view = wb.get_case(ref)
        refund = [r for r in case_view["refunds"]
                  if r["refund_ref"] == rfd["refund_ref"]][0]
        self.assertEqual(refund["status"], M.RF_CONFIRMED)
        # 回执 ack 掉在途退款请求
        self.assertNotIn(refund_req["request_uid"],
                         [r["request_uid"] for r in wb.list_unfinished_requests()])

        # 6) 相同回执重复到达：不得重复通知
        before = wb.db.query_one("SELECT COUNT(*) AS c FROM notifications")["c"]
        with self.assertRaises(DuplicateReceipt):
            wb.receive_bank_receipt(
                ref, "RCP-20260919-0007", M.RECEIPT_REFUND, receipt_payload,
                "acquirer-gw", event_time=wb.now_iso(),
                currency="USD", amount_minor=4000)
        after = wb.db.query_one("SELECT COUNT(*) AS c FROM notifications")["c"]
        self.assertEqual(before, after)

        # 7) 最终结论：部分退款了结
        final = wb.resolve_case(ref, M.RESOLUTION_PARTIAL_REFUND,
                                "supervisor.wang", "补证齐全且银行确认部分退款")
        self.assertEqual(final["status"], M.CASE_RESOLVED)
        self.assertEqual(final["resolution"], M.RESOLUTION_PARTIAL_REFUND)

        # 8) 时限：补证第二次在时限内完成；没有逾期
        self.assertEqual(
            [d["status"] for d in final["deadlines"] if d["subject_ref"] == ev1["request_ref"]][0],
            M.DL_MET)
        self.assertEqual(
            [d["status"] for d in final["deadlines"] if d["subject_ref"] == ev2["request_ref"]][0],
            M.DL_MET)

        # 9) 责任链完整有序
        chain = [(h["kind"], h["from_owner"], h["to_owner"])
                 for h in final["responsibility_chain"]]
        self.assertEqual(chain, [
            (M.HANDOVER_TRANSFER, "-", "agent.li"),
            (M.HANDOVER_TRANSFER, "agent.li", "agent.chen"),
            (M.HANDOVER_ESCALATION, "agent.chen", "supervisor.wang"),
        ])

        # 10) 通知去重：同一回执每角色恰好一条
        rows = wb.db.query_all(
            "SELECT receipt_uid,target_role,COUNT(*) AS c FROM notifications "
            "GROUP BY receipt_uid,target_role")
        self.assertTrue(rows)
        self.assertTrue(all(r["c"] == 1 for r in rows))

        # 11) 审计导出：包含关键事件且按时间有序
        export = wb.export_audit(ref)
        types = [e["event_type"] for e in export["events"]]
        for expected in [
            "CASE_CREATED", "EVIDENCE_REQUESTED", "EVIDENCE_SUBMITTED",
            "EVIDENCE_DECIDED", "REFUND_PROPOSED", "REFUND_APPROVED",
            "REFUND_SENT_TO_BANK", "RECEIPT_RECEIVED", "RECEIPT_DUPLICATE_IGNORED",
            "CASE_TRANSFERRED", "CASE_ESCALATED", "CASE_RESOLVED",
        ]:
            self.assertIn(expected, types, f"审计缺少 {expected}")
        # 事件流按写入顺序（id 递增）导出
        self.assertEqual(types, [e["event_type"] for e in
                                 sorted(export["events"], key=lambda e: e["id"])])
        self.assertEqual(export["case"]["resolution"], M.RESOLUTION_PARTIAL_REFUND)
        self.assertEqual(export["event_count"], len(export["events"]))

        # 12) 程序再次运行：无未完成外部请求；状态全部落盘可恢复
        self.db.close()
        db2 = Database(self.path)
        wb2 = Workbench(db2, gateway=ScriptedGateway(), clock=clock)
        recovery = wb2.recover_external_requests()
        self.assertEqual(recovery["still_pending"], 0)
        reopened = wb2.get_case(ref, M.ROLE_SUPERVISOR)
        self.assertEqual(reopened["status"], M.CASE_RESOLVED)
        self.assertEqual(len(reopened["receipts"]), 1)  # 重复回执未落第二条
        self.assertEqual(len(reopened["responsibility_chain"]), 3)
        db2.close()
