from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.recommendation.market_data import FakeSession
from trader.recommendation.infra.market_data.providers.tencent import TencentClient


_SOURCE_TIME = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _body(timestamp: str) -> bytes:
    fields = [""] * 50
    fields[1:7] = ["测试股份", "600001", "12.00", "11.65", "11.80", "250000"]
    fields[30] = timestamp
    return f'v_sh600001="{"~".join(fields)}";'.encode("gb18030")


@pytest.mark.parametrize("timestamp", ("", " ", "bad-time", "20260230100000", "2026071610000", "202607161000000"))
@pytest.mark.parametrize("mode", ("targeted", "market"))
def test_tencent_rejects_unknown_or_invalid_source_time_instead_of_using_receipt(timestamp, mode):
    received_at = _SOURCE_TIME + timedelta(hours=4, minutes=50)
    client = TencentClient(
        worker_pool=None, timeout_seconds=2,
        session_factory=lambda: FakeSession([_body(timestamp)]), wall_clock=lambda: received_at,
    )
    with pytest.raises(RuntimeError, match="no usable candidate quotes"):
        if mode == "targeted":
            client.fetch_quotes(("600001",))
        else:
            client.fetch_market(("600001",))


@pytest.mark.parametrize("mode", ("targeted", "market"))
def test_tencent_preserves_old_supplier_time_and_actual_receipt_time(mode):
    received_at = _SOURCE_TIME + timedelta(hours=4, minutes=50)
    client = TencentClient(
        worker_pool=None, timeout_seconds=2,
        session_factory=lambda: FakeSession([_body("20260716100000")]), wall_clock=lambda: received_at,
    )
    quote, = client.fetch_quotes(("600001",)) if mode == "targeted" else client.fetch_market(("600001",))
    assert quote.source_time == _SOURCE_TIME
    assert quote.received_time == received_at
    assert quote.age_seconds(received_at) == 17400
