from __future__ import annotations

from argparse import Namespace

from scripts.runtime_diagnostics.history_daily_capability import (
    BaoStockProbe,
    build_report,
    minimum_baostock_duration,
)


def test_baostock_lower_bound_counts_both_raw_and_qfq_per_security_queries() -> None:
    estimate = minimum_baostock_duration(5453, query_interval_seconds=2.0)

    assert estimate.security_count == 5453
    assert estimate.minimum_single_side_calls == 5453
    assert estimate.minimum_raw_qfq_calls == 10906
    assert estimate.minimum_single_side_seconds == 10904.0
    assert estimate.minimum_raw_qfq_seconds == 21810.0


def test_capability_gate_fails_closed_when_no_configured_source_is_bulk_and_training_ready() -> None:
    report = build_report(
        Namespace(universe_size=5453, runtime_config="config/runtime.json"),
        tushare_points=120,
        tushare_enabled=True,
        baostock=BaoStockProbe(
            status="passed",
            raw_rows=2,
            qfq_rows=2,
            raw_adjustflag="3",
            qfq_adjustflag="2",
            includes_trading_status=True,
            latest_trade_date="2026-09-09",
            error=None,
        ),
    )

    assert report["status"] == "degraded"
    assert report["decision"] == {
        "status": "blocked",
        "selected_baseline_source": "baostock",
        "efficient_daily_source": None,
        "blockers": [
            "configured_tushare_lacks_adjustment_factor_access",
            "baostock_requires_per_security_raw_and_qfq_queries",
            "tencent_and_eastmoney_have_no_validated_market_day_batch_contract",
        ],
    }
    candidates = {item["source"]: item for item in report["candidates"]}
    assert candidates["baostock"]["raw_qfq_semantics_observed"] is True
    assert candidates["baostock"]["market_day_batch"] is False
    assert candidates["tushare"]["market_day_raw"] is True
    assert candidates["tushare"]["adjustment_factor"] is False
    assert candidates["tencent_eastmoney"]["code_change_contract"] is False
    assert "prices" not in str(report).lower()


def test_capability_report_does_not_claim_baostock_semantics_when_probe_failed() -> None:
    report = build_report(
        Namespace(universe_size=10, runtime_config="config/runtime.json"),
        tushare_points=2000,
        tushare_enabled=True,
        baostock=BaoStockProbe(
            status="failed",
            raw_rows=0,
            qfq_rows=0,
            raw_adjustflag=None,
            qfq_adjustflag=None,
            includes_trading_status=False,
            latest_trade_date=None,
            error="supplier_login_network_failed",
        ),
    )

    candidates = {item["source"]: item for item in report["candidates"]}
    assert candidates["baostock"]["raw_qfq_semantics_observed"] is False
    assert candidates["tushare"]["adjustment_factor"] is True
    assert report["decision"]["status"] == "blocked"


def test_disabled_tushare_is_not_reported_as_an_available_market_day_source() -> None:
    report = build_report(
        Namespace(universe_size=10, runtime_config="/private/runtime.json"),
        tushare_points=2000,
        tushare_enabled=False,
        baostock=BaoStockProbe("failed", 0, 0, None, None, False, None, "supplier_call_timeout"),
    )

    candidates = {item["source"]: item for item in report["candidates"]}
    assert candidates["tushare"]["market_day_raw"] is False
    assert report["decision"]["blockers"][0] == "configured_tushare_unavailable"
    assert "/private/runtime.json" not in str(report)
