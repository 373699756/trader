from __future__ import annotations

import json
from datetime import date

import pytest

from trader.infra.research import baostock_gap_supplier as supplier


def _payload(code: str = "600001", day: str = "2026-09-02") -> str:
    value = {
        "adjustment": "qfq",
        "amount": 1000.0,
        "close_price": 10.0,
        "code": code,
        "high_price": 10.2,
        "low_price": 9.8,
        "open_price": 9.9,
        "pct_change": None,
        "preclose": None,
        "trade_date": day,
        "trading_status": "trading",
        "turnover": None,
        "volume": 100.0,
    }
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def test_gap_values_are_typed_canonical_and_deterministically_ordered() -> None:
    request = supplier.BaoStockGapRequest(
        "600001",
        "daily_qfq",
        (date(2026, 9, 2), date(2026, 9, 1), date(2026, 9, 2)),
    )
    record = supplier.BaoStockGapRecord("600001", date(2026, 9, 2), "daily_qfq", _payload())
    unavailable = supplier.BaoStockGapUnavailable(
        "600001",
        date(2026, 9, 1),
        "daily_qfq",
        "supplier_adjustment_unavailable",
    )

    result = supplier.BaoStockGapResult((record,), (unavailable,))

    assert request.trade_dates == (date(2026, 9, 1), date(2026, 9, 2))
    assert len(record.content_hash) == 64
    assert result.records == (record,)


def test_gap_record_rejects_payload_identity_or_noncanonical_json() -> None:
    with pytest.raises(ValueError, match="identity"):
        supplier.BaoStockGapRecord("600002", date(2026, 9, 2), "daily_qfq", _payload())
    with pytest.raises(ValueError, match="identity"):
        supplier.BaoStockGapRecord(
            "600001",
            date(2026, 9, 2),
            "daily_qfq",
            json.dumps(json.loads(_payload()), indent=2),
        )


def test_empty_gap_fetch_does_not_start_a_supplier_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_context(_method: str):
        raise AssertionError("empty requests must not start a child process")

    monkeypatch.setattr(supplier, "get_context", unexpected_context)

    assert supplier.fetch_baostock_gaps(()) == supplier.BaoStockGapResult((), ())


@pytest.mark.parametrize(
    ("retries", "timeout"),
    ((3, 60.0), (2, 0.0)),
)
def test_gap_fetch_rejects_unbounded_worker_options(retries: int, timeout: float) -> None:
    request = supplier.BaoStockGapRequest("600001", "daily_qfq", (date(2026, 9, 2),))

    with pytest.raises(ValueError, match="options"):
        supplier.fetch_baostock_gaps((request,), retries=retries, call_timeout_seconds=timeout)
