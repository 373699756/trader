from datetime import date, timedelta

import pytest

from trader.domain.research.baostock_daily import (
    BAOSTOCK_SOURCE_CUTOFF,
    BaoStockBoardCoverage,
    BaoStockCalendar,
    BaoStockCodeCoverage,
    BaoStockDailySide,
    BaoStockDailySpec,
    BaoStockSecurity,
    build_baostock_coverage_audit,
    build_baostock_v3_split,
    join_baostock_daily_sides,
)


def _calendar(count: int) -> BaoStockCalendar:
    start = BAOSTOCK_SOURCE_CUTOFF - timedelta(days=count - 1)
    return BaoStockCalendar(tuple(start + timedelta(days=offset) for offset in range(count)))


def _side(day: date, adjustment: str, *, trading_status: str = "trading") -> BaoStockDailySide:
    return BaoStockDailySide(
        code="600001",
        trade_date=day,
        adjustment=adjustment,
        open_price=10.0,
        high_price=10.5,
        low_price=9.8,
        close_price=10.2,
        volume=100.0,
        amount=1_000.0,
        preclose=9.9 if adjustment == "unadjusted" else None,
        pct_change=3.03 if adjustment == "unadjusted" else None,
        turnover=1.2 if adjustment == "unadjusted" else None,
        trading_status=trading_status,
    )


def test_baostock_spec_rejects_more_than_2000_sessions_and_keeps_identity() -> None:
    spec = BaoStockDailySpec()

    assert spec.research_identity == "score_baostock_daily_core_v2"
    assert spec.sessions == 2000
    assert spec.production_authority is False
    assert spec.point_in_time_parity is False
    with pytest.raises(ValueError, match=r"\[1, 2000\]"):
        BaoStockDailySpec(sessions=2001)


def test_coverage_values_reject_inconsistent_ratios_and_empty_population_eligibility() -> None:
    with pytest.raises(ValueError, match="ratio"):
        BaoStockBoardCoverage("main", 10, 5, 0.75)
    with pytest.raises(ValueError, match="ratio"):
        BaoStockCodeCoverage("600001", 10, 5, 0.75, False)
    with pytest.raises(ValueError, match="eligibility"):
        BaoStockCodeCoverage("600001", 0, 0, 1.0, True)


def test_raw_and_qfq_are_one_logical_cell_and_missing_side_is_explicit() -> None:
    days = (date(2026, 8, 29), date(2026, 8, 30))

    batch = join_baostock_daily_sides(
        "600001",
        days,
        (_side(days[0], "unadjusted"), _side(days[1], "unadjusted")),
        (_side(days[0], "qfq"),),
    )

    assert len(batch.cells) == 2
    assert batch.cells[0].status == "complete"
    assert batch.cells[0].unadjusted is not None
    assert batch.cells[0].qfq is not None
    assert batch.cells[1].status == "qfq_missing"
    assert batch.cells[1].unadjusted is not None
    assert batch.cells[1].qfq is None


def test_supplier_marked_suspension_can_preserve_empty_market_fields() -> None:
    day = date(2026, 8, 29)

    def suspended(adjustment: str) -> BaoStockDailySide:
        return BaoStockDailySide(
            "600001",
            day,
            adjustment,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "suspended",
        )

    batch = join_baostock_daily_sides(
        "600001",
        (day,),
        (suspended("unadjusted"),),
        (suspended("qfq"),),
    )

    assert batch.cells[0].status == "supplier_marked_suspended"
    assert batch.cells[0].obtained is True


def test_coverage_uses_listing_and_delisting_dates_not_a_common_intersection() -> None:
    spec = BaoStockDailySpec(sessions=5)
    calendar = _calendar(5)
    old = BaoStockSecurity("600001", "Old", "main", calendar.open_dates[0], None, "fixture")
    recent = BaoStockSecurity("300001", "Recent", "chinext", calendar.open_dates[3], None, "fixture")
    delisted = BaoStockSecurity("688001", "Past", "star", calendar.open_dates[0], calendar.open_dates[3], "fixture")
    batches = (
        join_baostock_daily_sides(
            old.code,
            calendar.expected_dates(old),
            tuple(_side_for(old.code, day, "unadjusted") for day in calendar.expected_dates(old)),
            tuple(_side_for(old.code, day, "qfq") for day in calendar.expected_dates(old)),
        ),
        join_baostock_daily_sides(
            recent.code,
            calendar.expected_dates(recent),
            tuple(_side_for(recent.code, day, "unadjusted") for day in calendar.expected_dates(recent)),
            tuple(_side_for(recent.code, day, "qfq") for day in calendar.expected_dates(recent)),
        ),
        join_baostock_daily_sides(
            delisted.code,
            calendar.expected_dates(delisted),
            tuple(_side_for(delisted.code, day, "unadjusted") for day in calendar.expected_dates(delisted)),
            tuple(_side_for(delisted.code, day, "qfq") for day in calendar.expected_dates(delisted)),
        ),
    )

    audit = build_baostock_coverage_audit(spec, calendar, (old, recent, delisted), batches)

    assert audit.expected_cells == 10
    assert audit.obtained_cells == 10
    assert audit.all_cell_coverage == 1.0
    assert {item.board: item.coverage_ratio for item in audit.board_coverages} == {
        "main": 1.0,
        "chinext": 1.0,
        "star": 1.0,
    }
    assert {item.code: item.expected_cells for item in audit.code_coverages} == {
        "300001": 2,
        "600001": 5,
        "688001": 3,
    }
    assert audit.failed_codes == ()
    assert audit.status == "historical_data_insufficient"
    assert "authoritative_calendar_below_2000" in audit.failure_reasons


def test_v3_split_permanently_reserves_latest_200_dates() -> None:
    start = date(2021, 1, 1)
    dates = tuple(start + timedelta(days=offset) for offset in range(1250))

    split = build_baostock_v3_split(dates, parent_manifest_hash="a" * 64)

    assert len(split.development_dates) >= 600
    assert len(split.confirmation_dates) >= 200
    assert len(split.daily_proxy_holdout_dates) >= 200
    assert len(split.point_in_time_holdout_dates) == 200
    assert split.point_in_time_holdout_dates == dates[-200:]
    assert len(split.first_embargo_dates) == 5
    assert len(split.second_embargo_dates) == 5
    assert len(split.early_stopping_dates) == 20
    assert len(split.calibration_dates) == 20
    assert not set(split.point_in_time_holdout_dates).intersection(split.development_dates)
    assert split.terminal_holdout_opened is False
    assert split.production_authority is False


def _side_for(code: str, day: date, adjustment: str) -> BaoStockDailySide:
    side = _side(day, adjustment)
    return BaoStockDailySide(
        code,
        side.trade_date,
        side.adjustment,
        side.open_price,
        side.high_price,
        side.low_price,
        side.close_price,
        side.volume,
        side.amount,
        side.preclose,
        side.pct_change,
        side.turnover,
        side.trading_status,
    )
