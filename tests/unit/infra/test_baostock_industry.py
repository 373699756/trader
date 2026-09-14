from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.infra.market_data.providers.baostock_industry import (
    BaoStockIndustryClient,
    BaoStockIndustryRow,
)

NOW = datetime(2026, 9, 14, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_baostock_industry_client_keeps_only_current_csrc_classification() -> None:
    rows = (
        BaoStockIndustryRow("sh.600000", "J66货币金融服务", "证监会行业分类", "2026-09-07"),
        BaoStockIndustryRow("sz.000001", "J66货币金融服务", "证监会行业分类", "2026-09-07"),
        BaoStockIndustryRow("sh.600001", "", "证监会行业分类", "2026-09-07"),
        BaoStockIndustryRow("sz.000002", "房地产", "申万一级行业", "2026-09-07"),
    )
    client = BaoStockIndustryClient(
        fetch_rows=lambda _date, _timeout: rows,
        timeout_seconds=3.0,
        minimum_rows=2,
    )

    observations = client.fetch(NOW)

    assert tuple(item.subject_key for item in observations) == ("000001", "600000")
    assert observations[0].fields == {
        "model_industry": "J66货币金融服务",
        "model_industry_classification": "证监会行业分类",
        "model_industry_data_version": observations[0].data_version,
        "model_industry_effective_date": "2026-09-07",
        "model_industry_source": "baostock",
    }
    status = client.health()
    assert status.planned_count == status.success_count == 1
    assert status.snapshot_rows == 2
    assert status.invalid_rows == 2
    assert status.last_error is None


def test_baostock_industry_client_reports_bounded_failure_without_erasing_last_snapshot() -> None:
    calls = 0

    def fetch_rows(_date: str, _timeout: float) -> tuple[BaoStockIndustryRow, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (BaoStockIndustryRow("sh.600000", "J66货币金融服务", "证监会行业分类", "2026-09-07"),)
        raise TimeoutError("supplier timed out")

    client = BaoStockIndustryClient(fetch_rows=fetch_rows, timeout_seconds=3.0, minimum_rows=1)
    assert len(client.fetch(NOW)) == 1

    with pytest.raises(TimeoutError):
        client.fetch(NOW)

    status = client.health()
    assert status.planned_count == 2
    assert status.success_count == 1
    assert status.error_count == status.timeout_count == 1
    assert status.snapshot_rows == 1
    assert status.last_error == "TimeoutError"


def test_baostock_industry_client_rejects_a_partial_whole_market_snapshot() -> None:
    client = BaoStockIndustryClient(
        fetch_rows=lambda _date, _timeout: (
            BaoStockIndustryRow("sh.600000", "J66货币金融服务", "证监会行业分类", "2026-09-07"),
        ),
        timeout_seconds=3.0,
        minimum_rows=2,
    )

    with pytest.raises(ValueError, match="incomplete"):
        client.fetch(NOW)

    assert client.health().snapshot_rows == 0
