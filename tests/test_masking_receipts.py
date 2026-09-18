import unittest

from service import models as M
from service.masking import redact
from service.errors import ImmutableReceipt
from tests.helpers import case_payload, make_workbench


class MaskingTest(unittest.TestCase):
    def test_role_based_redaction(self):
        obj = {
            "customer_name": "Zhang Wei",
            "customer_email": "zhang.wei@example.com",
            "card_last4": "4242",
        }
        # 主管看明文
        sup = redact(obj, M.ROLE_SUPERVISOR)
        self.assertEqual(sup["customer_name"], "Zhang Wei")
        self.assertEqual(sup["customer_email"], "zhang.wei@example.com")
        # 一线：姓名明文、邮箱局部掩码、卡显示 ****4242
        agent = redact(obj, M.ROLE_AGENT)
        self.assertEqual(agent["customer_name"], "Zhang Wei")
        self.assertEqual(agent["customer_email"], "z***@example.com")
        self.assertEqual(agent["card_last4"], "****4242")
        # 收单行/商户只可见姓氏字母 + **
        bank = redact(obj, M.ROLE_BANK)
        self.assertEqual(bank["customer_name"], "Z**")
        # 翻译角色全部掩码
        tr = redact(obj, M.ROLE_TRANSLATOR)
        self.assertEqual(tr["customer_name"], "***")
        self.assertEqual(tr["customer_email"], "***@masked")
        self.assertEqual(tr["card_last4"], "****")

    def test_case_view_is_redacted_per_role(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        ref = case["case_ref"]
        agent_view = wb.get_case(ref, M.ROLE_AGENT)
        translator_view = wb.get_case(ref, M.ROLE_TRANSLATOR)
        self.assertEqual(agent_view["customer_email"], "z***@example.com")
        self.assertEqual(translator_view["customer_name"], "***")
        # 库内仍为原文（脱敏只发生在视图/通知层）
        raw = wb.db.query_one("SELECT customer_email FROM cases WHERE id=?", (case["id"],))
        self.assertEqual(raw["customer_email"], "zhang.wei@example.com")


class ReceiptImmutabilityTest(unittest.TestCase):
    def test_original_receipt_cannot_be_modified(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        wb.receive_bank_receipt(case["case_ref"], "RCP-1", M.RECEIPT_GENERIC,
                                '{"note":"原始金额 100.00"}', "bank")
        with self.assertRaises(ImmutableReceipt):
            wb.update_receipt_payload("RCP-1", {"amount": 90})
        raw = wb.db.query_one("SELECT payload_text FROM bank_receipts WHERE receipt_uid=?",
                              ("RCP-1",))
        self.assertEqual(raw["payload_text"], '{"note":"原始金额 100.00"}')

    def test_correction_is_appended_with_reason_and_audit(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        wb.receive_bank_receipt(case["case_ref"], "RCP-1", M.RECEIPT_GENERIC, "orig", "bank")
        view = wb.correct_receipt("RCP-1", {"amount_minor": 9000},
                                  "回执解析有误，正确为部分退款确认", "agent.li")
        self.assertEqual(len(view["corrections"]), 1)
        self.assertEqual(view["corrections"][0]["reason"],
                         "回执解析有误，正确为部分退款确认")
        # 原始回执内容保持不变，更正有独立审计事件
        raw = wb.db.query_one("SELECT payload_text FROM bank_receipts WHERE receipt_uid=?",
                              ("RCP-1",))
        self.assertEqual(raw["payload_text"], "orig")
        events = [e["event_type"] for e in wb.export_audit(case["case_ref"])["events"]]
        self.assertIn("RECEIPT_CORRECTED", events)

    def test_receipt_payload_masked_for_translator_role(self):
        wb, _ = make_workbench()
        case = wb.create_case(case_payload(), "agent.li")
        # JSON 回执内含敏感字段：翻译角色看到脱敏值
        wb.receive_bank_receipt(case["case_ref"], "RCP-J", M.RECEIPT_GENERIC,
                                '{"customer_name":"Zhang Wei","note":"ok"}', "bank")
        tr_view = wb.get_case(case["case_ref"], M.ROLE_TRANSLATOR)["receipts"][0]
        self.assertIn("***", tr_view["payload_text"])
        self.assertNotIn("Zhang Wei", tr_view["payload_text"])
        # 主管看到原始 JSON
        sup_view = wb.get_case(case["case_ref"], M.ROLE_SUPERVISOR)["receipts"][0]
        self.assertIn("Zhang Wei", sup_view["payload_text"])
        # 库内原文不变
        raw = wb.db.query_one("SELECT payload_text FROM bank_receipts WHERE receipt_uid=?",
                              ("RCP-J",))
        self.assertEqual(raw["payload_text"], '{"customer_name":"Zhang Wei","note":"ok"}')
