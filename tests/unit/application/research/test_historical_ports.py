from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.application.ports.market import DataPlaneReadPort, MarketDataPlaneSnapshot
from trader.application.research.models import (
    AdjustmentFactorWindow,
    BoardPointInTimeCoverage,
    HardFilterAggregate,
    HistoricalDailyBar,
    HistoricalDaySummary,
    HistoricalFullCandidate,
    HistoricalFullFieldBundle,
    HistoricalMinuteBar,
    HistoricalSettlementEvidence,
)
from trader.application.research.ports import HistoricalDataPlaneReadPort
from trader.domain.research.historical import (
    CostSettlementBasis,
    HistoricalCandidateSummary,
    ResearchDataLineage,
    ScoreComponent,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 8, 10)
OBSERVED_AT = datetime(2026, 8, 10, 14, 50, tzinfo=SHANGHAI)


def _lineage(version: str, at: datetime = OBSERVED_AT) -> ResearchDataLineage:
    return ResearchDataLineage(
        source="v2_data_plane",
        source_time=at,
        received_at=at,
        quality_status="complete",
        content_version=version,
        content_hash="a" * 64,
    )


def _candidate(code: str, board: str) -> HistoricalCandidateSummary:
    return HistoricalCandidateSummary(
        code=code,
        board=board,
        feature_as_of=OBSERVED_AT,
        lineage=_lineage(f"candidate-{code}"),
        candidate_components=(ScoreComponent("candidate", 1.0, 80.0),),
        final_components=(ScoreComponent("final", 1.0, None),),
    )


def _summary() -> HistoricalDaySummary:
    return HistoricalDaySummary(
        trade_date=TRADE_DATE,
        observed_at=OBSERVED_AT,
        daily_feature_pack_version="daily:2026-08-10:1",
        market_epoch_version="market:2026-08-10:1",
        candidate_quote_epoch_version="candidate:2026-08-10:1",
        research_epoch_version="research:2026-08-10:1",
        input_hash="a" * 64,
        config_version="config-v1",
        calendar_version="calendar-v1",
        rule_versions=("rules-v1",),
        candidates=(
            _candidate("688001", "star"),
            _candidate("600001", "main"),
            _candidate("300001", "chinext"),
        ),
        hard_filter_aggregates=(HardFilterAggregate("main", "st_or_delisting", 2),),
        board_coverages=tuple(BoardPointInTimeCoverage(board, True, True) for board in ("main", "chinext", "star")),
        source_versions=(("market", "market-v1"), ("daily", "daily-v1")),
    )


def _bundle(codes: tuple[str, ...]) -> HistoricalFullFieldBundle:
    board_by_code = {"600001": "main", "300001": "chinext", "688001": "star"}
    candidates = tuple(
        HistoricalFullCandidate(
            code,
            board_by_code[code],
            OBSERVED_AT,
            {"feature": 80.0},
            _lineage(f"full-{code}"),
        )
        for code in codes
    )
    daily = tuple(
        HistoricalDailyBar(
            code=code,
            session_date=TRADE_DATE - timedelta(days=1),
            open_price=10.0,
            high=11.0,
            low=9.0,
            close=10.5,
            volume=1000.0,
            amount=10_000.0,
            adjustment_window_id=f"window-{code}",
            lineage=_lineage("daily-v1", OBSERVED_AT - timedelta(days=1)),
        )
        for code in codes
    )
    minute = tuple(
        HistoricalMinuteBar(
            code=code,
            minute=OBSERVED_AT - timedelta(minutes=1),
            close=10.5,
            volume=100.0,
            amount=1000.0,
            lineage=_lineage("minute-v1", OBSERVED_AT - timedelta(minutes=1)),
        )
        for code in codes
    )
    windows = tuple(
        AdjustmentFactorWindow(
            window_id=f"window-{code}",
            code=code,
            as_of=OBSERVED_AT - timedelta(days=1),
            factors=((TRADE_DATE - timedelta(days=1), 1.0),),
            lineage=_lineage("factor-v1", OBSERVED_AT - timedelta(days=1)),
        )
        for code in codes
    )
    settlements = tuple(
        HistoricalSettlementEvidence(
            basis=CostSettlementBasis(
                code,
                board_by_code[code],
                TRADE_DATE,
                TRADE_DATE + timedelta(days=1),
                0.03,
                -0.5,
                0.2,
            ),
            lineage=_lineage("settlement-v1", OBSERVED_AT + timedelta(days=1)),
        )
        for code in codes
    )
    return HistoricalFullFieldBundle(
        TRADE_DATE,
        "a" * 64,
        codes,
        candidates,
        daily,
        minute,
        windows,
        settlements,
        ("main", "chinext", "star"),
    )


def test_day_summary_is_sorted_point_in_time_and_hard_rejects_have_no_identity_field() -> None:
    summary = _summary()

    assert tuple(item.code for item in summary.candidates) == ("300001", "600001", "688001")
    assert summary.source_versions == (("daily", "daily-v1"), ("market", "market-v1"))
    assert {item.name for item in fields(HardFilterAggregate)} == {"board", "reason", "count"}


def test_day_summary_rejects_future_candidates_and_incomplete_board_coverage() -> None:
    future = replace(_summary().candidates[0], feature_as_of=OBSERVED_AT + timedelta(seconds=1))
    with pytest.raises(ValueError, match="future candidate"):
        replace(_summary(), candidates=(future, *_summary().candidates[1:]))
    with pytest.raises(ValueError, match="all three boards"):
        replace(_summary(), board_coverages=(BoardPointInTimeCoverage("main", True, True),))


def test_full_bundle_matches_requested_codes_and_deduplicates_shared_inputs() -> None:
    codes = ("300001", "600001", "688001")
    bundle = _bundle(codes)
    duplicated = replace(
        bundle,
        daily_bars=(*bundle.daily_bars, bundle.daily_bars[0]),
        minute_bars=(*bundle.minute_bars, bundle.minute_bars[0]),
        adjustment_windows=(*bundle.adjustment_windows, bundle.adjustment_windows[0]),
    )

    assert duplicated.requested_codes == codes
    assert len(duplicated.daily_bars) == len(duplicated.minute_bars) == len(duplicated.adjustment_windows) == 3

    with pytest.raises(ValueError, match="sorted and unique"):
        replace(bundle, requested_codes=tuple(reversed(codes)))


def test_full_bundle_rejects_conflicts_missing_fields_and_wrong_settlement_date() -> None:
    bundle = _bundle(("300001", "600001", "688001"))
    conflict = replace(bundle.daily_bars[0], close=10.6)
    with pytest.raises(ValueError, match="conflicting content"):
        replace(bundle, daily_bars=(*bundle.daily_bars, conflict))
    with pytest.raises(ValueError, match="every requested code"):
        replace(bundle, minute_bars=bundle.minute_bars[1:])
    wrong_basis = replace(bundle.settlements[0].basis, decision_date=TRADE_DATE - timedelta(days=1))
    wrong_date = replace(bundle.settlements[0], basis=wrong_basis)
    with pytest.raises(ValueError, match="decision dates"):
        replace(bundle, settlements=(wrong_date, *bundle.settlements[1:]))

    extra_window = replace(bundle.adjustment_windows[0], window_id="second-window")
    with pytest.raises(ValueError, match="adjustment windows for every requested code"):
        replace(bundle, adjustment_windows=(*bundle.adjustment_windows, extra_window))


def test_full_bundle_rejects_future_inputs_and_mismatched_settlement_boards() -> None:
    bundle = _bundle(("300001", "600001", "688001"))
    future_minute = replace(bundle.minute_bars[0], minute=OBSERVED_AT + timedelta(minutes=1))
    with pytest.raises(ValueError, match="candidate cutoff"):
        replace(bundle, minute_bars=(future_minute, *bundle.minute_bars[1:]))
    late_daily = replace(
        bundle.daily_bars[0],
        lineage=_lineage("late-daily", OBSERVED_AT + timedelta(minutes=1)),
    )
    with pytest.raises(ValueError, match="candidate cutoff"):
        replace(bundle, daily_bars=(late_daily, *bundle.daily_bars[1:]))
    incomplete_daily = replace(
        bundle.daily_bars[0],
        session_date=TRADE_DATE,
        lineage=_lineage("same-day-daily", OBSERVED_AT - timedelta(minutes=1)),
    )
    with pytest.raises(ValueError, match="completed before the trade date"):
        replace(bundle, daily_bars=(incomplete_daily, *bundle.daily_bars[1:]))
    wrong_board = replace(bundle.settlements[0].basis, board="main")
    with pytest.raises(ValueError, match="boards must match"):
        replace(bundle, settlements=(replace(bundle.settlements[0], basis=wrong_board), *bundle.settlements[1:]))


class _PortDouble:
    def snapshot(self) -> MarketDataPlaneSnapshot:
        return MarketDataPlaneSnapshot(None, None, None, None)

    def is_trading_day(self, trade_date: date) -> bool:
        return trade_date == TRADE_DATE

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary:
        assert trade_date == TRADE_DATE
        return _summary()

    def load_full_fields(self, trade_date: date, codes: tuple[str, ...]) -> HistoricalFullFieldBundle:
        assert trade_date == TRADE_DATE
        return _bundle(codes)


def _consume_port(port: HistoricalDataPlaneReadPort) -> tuple[str, ...]:
    summary = port.read_day_summary(TRADE_DATE)
    codes = tuple(item.code for item in summary.candidates)
    return port.load_full_fields(TRADE_DATE, codes).requested_codes


def test_two_phase_port_contract_can_be_implemented_without_a_provider_dependency() -> None:
    assert DataPlaneReadPort in HistoricalDataPlaneReadPort.__mro__
    assert _consume_port(_PortDouble()) == ("300001", "600001", "688001")
