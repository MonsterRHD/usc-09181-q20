"""转派与升级：责任链完整，并发转派只有一个生效。"""
import threading
import unittest

from service.domain import ConflictError, ValidationError

from tests.helpers import ServiceTestCase, open_case


class TransferChainTest(ServiceTestCase):
    def test_transfer_and_escalation_leave_chain(self):
        case = open_case(self.service)
        v = case["version"]
        self.service.transfer_case(case["id"], "agent-02", "跨境案件转双语客服", "agent-01", v)
        case = self.service.get_case(case["id"])
        self.service.escalate_case(case["id"], "dispute-team-lead", "商户拒绝协商",
                                   "agent-02", case["version"])

        view = self.service.get_case_view(case["id"], role="auditor")
        self.assertEqual(view["assignee"], "dispute-team-lead")
        self.assertEqual(view["escalation_level"], 1)
        chain = [(a["kind"], a["from_party"], a["to_party"], a["actor"], a["reason"])
                 for a in view["assignments"]]
        self.assertEqual(chain, [
            ("TRANSFER", "agent-01", "agent-02", "agent-01", "跨境案件转双语客服"),
            ("ESCALATION", "agent-02", "dispute-team-lead", "agent-02", "商户拒绝协商"),
        ])
        actions = [a["action"] for a in self.service.audit_trail(case["id"])]
        self.assertIn("CASE_TRANSFERRED", actions)
        self.assertIn("CASE_ESCALATED", actions)

    def test_transfer_requires_reason_and_version(self):
        case = open_case(self.service)
        with self.assertRaises(ValidationError):
            self.service.transfer_case(case["id"], "agent-02", "", "agent-01", case["version"])
        with self.assertRaises(ConflictError):
            self.service.transfer_case(case["id"], "agent-02", "转派", "agent-01", 999)

    def test_concurrent_transfer_only_one_wins(self):
        case = open_case(self.service)
        version = case["version"]
        results, errors = [], []

        def attempt(target):
            try:
                results.append(self.service.transfer_case(
                    case["id"], target, f"转给{target}", "scheduler", version))
            except ConflictError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(f"agent-{i:02d}",)) for i in range(2, 7)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1, "并发转派只能有一个生效")
        self.assertEqual(len(errors), 4)
        final = self.service.get_case(case["id"])
        self.assertEqual(final["version"], version + 1)
        # 责任链：一次成功转派 + 四次被拒绝的尝试都有审计记录
        view = self.service.get_case_view(case["id"], role="auditor")
        self.assertEqual(len(view["assignments"]), 1)
        self.assertEqual(view["assignments"][0]["to_party"], final["assignee"])
        rejected = [a for a in self.service.audit_trail(case["id"])
                    if a["action"] == "REASSIGN_REJECTED"]
        self.assertEqual(len(rejected), 4)


if __name__ == "__main__":
    unittest.main()
