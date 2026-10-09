from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.runtime_diagnostics.long_watchlist import audit_code, financial_admission
from trader.infra.settings import load_strategy_settings
from trader.recommendation.domain.market.research import FinancialReport

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("profit_field", ("parent_net_profit", "core_net_profit"))
@pytest.mark.parametrize("report_date", (date(2025, 12, 31), date(2026, 6, 30)))
def test_either_annual_or_latest_loss_is_excluded(profit_field: str, report_date: date) -> None:
    profits = {"parent_net_profit": 10.0, "core_net_profit": 10.0, profit_field: -1.0}
    report = FinancialReport(report_date, datetime(2026, 8, 20, tzinfo=SHANGHAI), **profits)

    result = financial_admission("000001", (report,), False, date(2026, 10, 9))

    assert result.disposition == "excluded"
    assert "latest_disclosed_loss" in result.reasons
    if report_date.month == 12:
        assert "historical_annual_loss" in result.reasons


def test_missing_latest_report_does_not_pass_using_an_old_cache_date() -> None:
    annual = FinancialReport(
        date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=SHANGHAI), parent_net_profit=10, core_net_profit=10
    )

    result = financial_admission("000001", (annual,), True, date(2026, 10, 9))

    assert result.disposition == "pending"
    assert "latest_report_missing" in result.reasons


def test_financial_exclusion_is_detected_without_announcement_cache(tmp_path: Path) -> None:
    financial = tmp_path / "raw" / "financial" / "000001.json"
    financial.parent.mkdir(parents=True)
    financial.write_text(
        json.dumps(
            {
                "observed_at": "2026-10-08T16:00:00+08:00",
                "payload": {
                    "result": {
                        "count": 1,
                        "data": [
                            {
                                "REPORT_DATE": "2025-12-31",
                                "NOTICE_DATE": "2026-03-20",
                                "PARENTNETPROFIT": -1,
                                "KCFJCXSYJLR": 10,
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    original = financial.read_bytes()

    result = audit_code(
        "000001",
        observed_at=datetime(2026, 10, 9, 16, tzinfo=SHANGHAI),
        policy=load_strategy_settings(PROJECT_ROOT / "config/strategy.json").long_research,
        timeout=1,
        exclusions={},
        evidence_caches=(tmp_path,),
    )

    assert result.disposition == "excluded"
    assert "historical_annual_loss" in result.reasons
    assert financial.read_bytes() == original
    assert tuple(tmp_path.rglob("*.json")) == (financial,)
