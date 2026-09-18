"""上线前验收：一笔交易完整经历补证、部分退款、跨日回执、并发转派，
核对最终结论、通知去重与审计导出。"""
import threading
import unittest
from datetime import datetime, timezone

from service.case_service import CaseService
from service.db import Store
from service.domain import CaseStatus, Resolution

from tests.helpers import CUSTOMER, TXN, ServiceTestCase


class LaunchAcceptanceTest(ServiceTestCase):
    def test_full_journey(self):
        # ---- 建案：一笔美国商户的跨境交易，客户授权后立案
        self.clock.now = datetime(2026, 9, 18, 15, 50, tzinfo=timezone.utc)  # 上海 23:50
        case = self.service.create_case(
            TXN, CUSTOMER, tz="Asia/Shanghai", actor="agent-01", assignee="agent-01")
        case_id = case["id"]
        self.service.set_authorization(case_id, True, channel="app", actor="customer")

        # ---- 补证：向商户/收单行要签购单，时限为上海时间次日 18:00
        req = self.service.create_evidence_request(
            case_id, "请提供签购单与配送凭证", actor="agent-01",
            due_local="2026-09-19T18:00:00", due_timezone="Asia/Shanghai")
        self.service.submit_evidence(req["id"], "签购单扫描件.pdf", actor="agent-01")
        self.service.submit_evidence(req["id"], "配送签收记录.pdf", actor="agent-01")
        self.service.fulfill_evidence_request(req["id"], actor="agent-01")

        # ---- 部分退款：商户同意退 40%，走完 提议->批准->结算
        refund = self.service.propose_refund(case_id, 40_000, "商户同意部分退款 40%", actor="agent-01")
        self.service.approve_refund(refund["id"], actor="supervisor-01")
        self.service.settle_refund(refund["id"], actor="system")

        # ---- 跨日回执：第一笔 9/18 23:55（上海），第二笔跨午夜后 9/19 00:10 到达
        self.clock.now = datetime(2026, 9, 18, 15, 55, tzinfo=timezone.utc)
        r1 = self.service.receive_receipt(case_id, "ACQUIRER", "acq-9001", {"stage": "受理"})
        self.clock.now = datetime(2026, 9, 18, 16, 10, tzinfo=timezone.utc)  # 上海 9/19 00:10
        r2 = self.service.receive_receipt(case_id, "ACQUIRER", "acq-9002", {"stage": "代位追偿完成"})
        self.assertEqual(r1["local_date"], "2026-09-18")
        self.assertEqual(r2["local_date"], "2026-09-19")
        # 回执内容有误：客服只能提交带原因的更正，原始回执不变
        self.service.correct_receipt(r2["id"], {"stage": "代位追偿完成", "arn": "7551"},
                                     "回执缺少 ARN，按收单行邮件补充", actor="agent-01")
        self.assertEqual(self.service.get_receipt(r2["id"])["payload"], {"stage": "代位追偿完成"})

        # ---- 重复回执：acq-9002 因对端重发再次到达，不得重复通知
        before = len(self.service.list_notifications(case_id))
        dup = self.service.receive_receipt(case_id, "ACQUIRER", "acq-9002", {"stage": "代位追偿完成"})
        self.assertTrue(dup["duplicate"])
        after = len(self.service.list_notifications(case_id))
        self.assertEqual(before, after, "重复回执不得产生新通知")

        # ---- 并发转派：两个坐席同时抢单，只有一个生效，责任链完整
        version = self.service.get_case(case_id)["version"]
        winners, losers = [], []
        barrier = threading.Barrier(2)

        def grab(target, bag_w, bag_l):
            barrier.wait()
            try:
                bag_w.append(self.service.transfer_case(
                    case_id, target, "双语坐席接手", "scheduler", version))
            except Exception as exc:  # noqa: BLE001
                bag_l.append(exc)

        t1 = threading.Thread(target=grab, args=("agent-02", winners, losers))
        t2 = threading.Thread(target=grab, args=("agent-03", winners, losers))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        # 升级到专家组
        current = self.service.get_case(case_id)
        self.service.escalate_case(case_id, "cross-border-experts", "涉及跨行追偿",
                                   current["assignee"], current["version"])

        # ---- 结论：部分退款已结算 40%，结论为 PARTIAL_REFUND
        final = self.service.resolve_case(
            case_id, Resolution.PARTIAL_REFUND, "商户部分退款 40%，剩余部分由收单行承担",
            actor="cross-border-experts")
        self.assertEqual(final["status"], CaseStatus.RESOLVED)
        self.assertEqual(final["resolution"], "PARTIAL_REFUND")

        # ---- 核对一：最终视图自洽
        view = self.service.get_case_view(case_id, role="auditor")
        self.assertEqual(view["assignee"], "cross-border-experts")
        self.assertEqual(view["escalation_level"], 1)
        self.assertEqual(len(view["receipts"]), 2)
        self.assertEqual(view["refunds"][0]["status"], "SETTLED")
        self.assertEqual(view["deadlines"][0]["status"], "MET")
        self.assertEqual(len(view["assignments"]), 2)  # 一次成功转派 + 一次升级

        # ---- 核对二：通知去重（同一回执只通知一次，退款/结论各一次）
        notices = self.service.list_notifications(case_id)
        receipt_notices = [n for n in notices if n["kind"] == "RECEIPT_RECEIVED"]
        self.assertEqual(len(receipt_notices), 2)
        self.assertEqual(len({n["dedup_key"] for n in notices}), len(notices),
                         "所有通知的 dedup_key 必须唯一")

        # ---- 核对三：审计导出按序覆盖关键节点
        actions = [a["action"] for a in self.service.audit_trail(case_id)]
        expected_order = [
            "CASE_CREATED", "AUTHORIZATION_GRANTED", "EVIDENCE_REQUESTED",
            "EVIDENCE_SUBMITTED", "EVIDENCE_SUBMITTED", "EVIDENCE_FULFILLED",
            "REFUND_PROPOSED", "REFUND_APPROVED", "REFUND_SETTLED",
            "RECEIPT_RECEIVED", "RECEIPT_RECEIVED", "RECEIPT_CORRECTED",
            "CASE_TRANSFERRED", "CASE_ESCALATED", "CASE_RESOLVED",
        ]
        pos = -1
        for expected in expected_order:
            nxt = next((i for i, a in enumerate(actions) if a == expected and i > pos), None)
            self.assertIsNotNone(nxt, f"审计链缺少事件: {expected}")
            pos = nxt
        rejected = [a for a in self.service.audit_trail(case_id) if a["action"] == "REASSIGN_REJECTED"]
        self.assertEqual(len(rejected), 1, "并发转派中失败的一次也应留痕")

        # ---- 核对四：重启后结论与待办依然可追踪
        restarted = CaseService(Store(self.db_path), clock=self.clock)
        restored = restarted.get_case_view(case_id, role="auditor")
        self.assertEqual(restored["resolution"], "PARTIAL_REFUND")
        self.assertEqual(len(restarted.audit_trail(case_id)), len(actions))
        self.assertEqual(restarted.pending_external_requests()[0]["type"], "RECEIPT_CORRECTION")


if __name__ == "__main__":
    unittest.main()
