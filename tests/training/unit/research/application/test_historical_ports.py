from __future__ import annotations

from dataclasses import fields, replace

from datetime import timedelta


import pytest


from trader.training.application.historical_extraction_models import BoardPointInTimeCoverage, HardFilterAggregate

from trader.training.application.research_ports import HistoricalDataPlaneReadPort


from tests.helpers.training.research.application.historical_ports import (
    OBSERVED_AT,
    TRADE_DATE,
    _PortDouble,
    _bundle,
    _consume_port,
    _lineage,
    _summary,
)


def test_day_summary_is_sorted_point_in_time_and_hard_rejects_have_no_identity_field() -> None:
    summary = _summary()

    assert tuple(item.code for item in summary.candidates) == ("300001", "600001", "688001")
    assert summary.source_versions == (("daily", "daily-current"), ("market", "market-initial"))
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
    wrong_basis = replace(bundle.settlements[0].basis, freeze_date=TRADE_DATE - timedelta(days=1))
    wrong_date = replace(bundle.settlements[0], basis=wrong_basis)
    with pytest.raises(ValueError, match="freeze dates"):
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


def test_two_phase_port_contract_can_be_implemented_without_a_provider_dependency() -> None:
    assert callable(HistoricalDataPlaneReadPort.snapshot)
    assert _consume_port(_PortDouble()) == ("300001", "600001", "688001")
