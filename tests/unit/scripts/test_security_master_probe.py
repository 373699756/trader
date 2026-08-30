from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from scripts.runtime_diagnostics.exchange_security_master import collect
from trader.infra.market_data.exchange_security_master import (
    ExchangeSecurityMasterClient,
    ExchangeSecurityMasterListing,
)


def test_security_master_probe_emits_only_bounded_coverage_counts() -> None:
    observed_at = datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    client = ExchangeSecurityMasterClient(
        timeout_seconds=2.0,
        minimum_rows=2,
        sse_fetcher=lambda _timeout: (
            ExchangeSecurityMasterListing("600001", "测试一", date(2020, 1, 2), "main", "SSE"),
        ),
        szse_fetcher=lambda _timeout: (
            ExchangeSecurityMasterListing("300001", "测试二", date(2021, 2, 3), "chinext", "SZSE"),
        ),
        wall_clock=lambda: observed_at,
    )

    report = collect(client, observed_at)

    assert report["status"] == "passed"
    assert report["summary"] == {
        "total_rows": 2,
        "listing_date_rows": 2,
        "coverage_ratio": 1.0,
        "exchange_rows": {"SSE": 1, "SZSE": 1},
        "board_rows": {"chinext": 1, "main": 1},
    }
    assert "600001" not in str(report)
    assert "测试一" not in str(report)
