"""敏感信息按角色脱敏。"""
import unittest

from tests.helpers import ServiceTestCase, open_case


class MaskingTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.case = open_case(self.service)

    def view(self, role):
        return self.service.get_case_view(self.case["id"], role=role)

    def test_agent_sees_masked_contacts_and_last4_card(self):
        view = self.view("agent")
        self.assertEqual(view["card_number"], "************7890")
        self.assertEqual(view["customer_name"], "张*")
        self.assertEqual(view["customer_email"], "z***@example.com")
        self.assertEqual(view["customer_phone"], "*********8000")

    def test_merchant_sees_minimum(self):
        view = self.view("merchant")
        self.assertEqual(view["card_number"], "************7890")
        self.assertEqual(view["customer_email"], "***")
        self.assertEqual(view["customer_phone"], "***")
        self.assertEqual(view["customer_name"], "张*")

    def test_translator_cannot_see_card_or_contacts(self):
        view = self.view("translator")
        self.assertEqual(view["card_number"], "***")
        self.assertEqual(view["customer_email"], "***")
        self.assertEqual(view["customer_phone"], "***")

    def test_auditor_sees_full_detail(self):
        view = self.view("auditor")
        self.assertEqual(view["card_number"], "6222021234567890")
        self.assertEqual(view["customer_name"], "张伟")
        self.assertEqual(view["customer_email"], "zhangwei@example.com")

    def test_unknown_role_gets_strictest_masking(self):
        view = self.view("intern")
        self.assertEqual(view["card_number"], "***")
        self.assertEqual(view["customer_email"], "***")
        self.assertEqual(view["customer_phone"], "***")

    def test_masking_does_not_touch_stored_data(self):
        self.view("merchant")
        stored = self.service.get_case(self.case["id"])
        self.assertEqual(stored["customer_email"], "zhangwei@example.com")


if __name__ == "__main__":
    unittest.main()
