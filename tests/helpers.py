"""测试用公共夹具：可控时钟 + 可编排故障的外部网关。"""
from datetime import datetime, timezone

from service.db import Database
from service.workbench import ExternalGateway, Workbench


class FakeClock:
    def __init__(self, dt: datetime | None = None):
        self.dt = (dt or datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc))

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, hours: float = 0, **kw) -> None:
        from datetime import timedelta
        if hours:
            self.dt += timedelta(hours=hours)
        if kw:
            self.dt += timedelta(**kw)


class ScriptedGateway(ExternalGateway):
    """前 fail_first 次 send 抛错；reconcile 可按 request_uid 返回结果。"""

    def __init__(self, *, fail_first: int = 0, reconcile: str = "ACKNOWLEDGED"):
        self.fail_first = fail_first
        self.default_reconcile = reconcile
        self.reconcile_map: dict[str, str] = {}
        self.sent: list[dict] = []

    def send(self, request: dict) -> bool:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ConnectionError("外部网关暂不可用")
        self.sent.append(request)
        return True

    def reconcile(self, request: dict) -> str:
        return self.reconcile_map.get(request["request_uid"], self.default_reconcile)


def make_workbench(path: str = ":memory:", *, gateway=None, clock=None) -> Workbench:
    clock = clock or FakeClock()
    return Workbench(Database(path), gateway=gateway or ScriptedGateway(), clock=clock), clock


def case_payload(**overrides) -> dict:
    base = {
        "txn_ref": "TXN-1001",
        "customer_name": "Zhang Wei",
        "customer_email": "zhang.wei@example.com",
        "card_last4": "4242",
        "currency": "USD",
        "amount_minor": 10000,
        "txn_time": "2026-09-15T08:30:00Z",
        "txn_timezone": "America/New_York",
        "merchant_name": "Global Gadgets NYC",
        "acquirer_ref": "ACQ-77",
        "reason_code": "10.4",
    }
    base.update(overrides)
    return base
