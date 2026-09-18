"""测试共享工具：可控时钟与临时数据库。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from service.case_service import CaseService
from service.db import Store


class FakeClock:
    """可手动推进的时钟，用于跨日回执与时限测试。"""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs):
        self.now = self.now + timedelta(**kwargs)


def make_service(start: datetime | None = None, db_path: str | None = None):
    """返回 (service, clock, db_path)；db_path 传入时用于模拟重启恢复。"""
    clock = FakeClock(start or datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc))
    if db_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db_path = tmp.name
    service = CaseService(Store(db_path), clock=clock)
    return service, clock, db_path


TXN = {
    "card_number": "6222021234567890",
    "amount_cents": 100_000,
    "currency": "USD",
    "merchant": "Paris Cafe",
    "merchant_country": "FR",
    "occurred_at": "2026-09-10T14:30:00+00:00",
}

CUSTOMER = {
    "name": "张伟",
    "email": "zhangwei@example.com",
    "phone": "+8613800138000",
}


def open_case(service: CaseService, tz: str = "Asia/Shanghai") -> dict:
    """建案并授权，返回案件视图。"""
    view = service.create_case(TXN, CUSTOMER, tz=tz, actor="agent-01", assignee="agent-01")
    service.set_authorization(view["id"], granted=True, channel="app", actor="customer")
    return service.get_case_view(view["id"], role="auditor")


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, self.db_path = make_service()
