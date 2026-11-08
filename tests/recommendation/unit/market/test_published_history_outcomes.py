"""Published outcome projections retain missing raw change without losing ordinary trades."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide
from trader.recommendation.domain.outcomes.evaluation import EvaluateOutcome, OutcomeEvaluationRequest
from trader.recommendation.domain.outcomes.models import (
    OutcomeExitStatus,
    OutcomePrice,
    OutcomeTarget,
    OutcomeTradingStatus,
    RecommendationOutcome,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.market_data.outcome_history import BuildOutcomeBar
from trader.recommendation.infra.market_data.published_history_bars import OutcomeBars

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REFERENCE_DATE = date(2018, 8, 7)
_SAMPLE_DATE = date(2018, 8, 8)


def _published_cell(
    trade_date: date,
    *,
    raw_prices: OutcomePrice,
    qfq_prices: OutcomePrice,
    pct_change: float | None,
) -> PublishedHistoryCell:
    raw = PublishedHistorySide(
        code="300118",
        trade_date=trade_date,
        adjustment="unadjusted",
        open_price=raw_prices.open_price,
        high_price=raw_prices.high,
        low_price=raw_prices.low,
        close_price=raw_prices.close,
        volume=1000.0,
        amount=20_000.0,
        preclose=None,
        pct_change=pct_change,
        turnover=0.08,
        trading_status="trading",
    )
    qfq = replace(
        raw,
        adjustment="qfq",
        open_price=qfq_prices.open_price,
        high_price=qfq_prices.high,
        low_price=qfq_prices.low,
        close_price=qfq_prices.close,
        volume=2000.0,
        amount=30_000.0,
        pct_change=None,
        turnover=None,
    )
    return PublishedHistoryCell(raw.code, trade_date, "complete", raw, qfq)


def _ordinary_cell(trade_date: date, *, close: float = 10.0) -> PublishedHistoryCell:
    return _published_cell(
        trade_date,
        raw_prices=OutcomePrice(10.0, 10.6, 9.8, close),
        qfq_prices=OutcomePrice(5.0, 5.3, 4.9, close / 2.0),
        pct_change=None,
    )


def _observed_one_price_cell() -> PublishedHistoryCell:
    # One published 300118/2018-08-08 observation; this does not assert a legal limit-down.
    cell = _published_cell(
        _SAMPLE_DATE,
        raw_prices=OutcomePrice(8.67, 8.67, 8.67, 8.67),
        qfq_prices=OutcomePrice(7.83, 7.83, 7.83, 7.83),
        pct_change=None,
    )
    assert cell.unadjusted is not None and cell.qfq is not None
    return replace(
        cell,
        unadjusted=replace(cell.unadjusted, volume=561_300.0, amount=4_866_500.0),
        qfq=replace(cell.qfq, volume=561_300.0, amount=4_866_500.0),
    )


def _evaluate(reference: PublishedHistoryCell, future: PublishedHistoryCell) -> RecommendationOutcome:
    assert reference.unadjusted is not None and reference.unadjusted.close_price is not None
    target = OutcomeTarget(
        snapshot_id="published-outcome-regression",
        strategy=Strategy.TOMORROW,
        recommend_date=reference.trade_date.isoformat(),
        stock_code=reference.code,
        anchor_raw_price=reference.unadjusted.close_price,
        atr20_pct=2.0,
        entry_at=datetime.combine(reference.trade_date, time(15), _SHANGHAI),
    )
    return EvaluateOutcome(
        OutcomeEvaluationRequest(
            target=target,
            bars=OutcomeBars((reference, future)),
            horizon=1,
            benchmark_returns=(0.0,),
            settled_at=datetime.combine(future.trade_date, time(16), _SHANGHAI),
            expected_trade_dates=(future.trade_date.isoformat(),),
        )
    )


def test_published_observed_one_price_missing_change_stays_unknown() -> None:
    cell = _observed_one_price_cell()

    bars = OutcomeBars((cell,))

    assert len(bars) == 1
    assert bars[0].trading_status is OutcomeTradingStatus.UNKNOWN
    assert bars[0].raw == OutcomePrice(8.67, 8.67, 8.67, 8.67)
    assert bars[0].qfq == OutcomePrice(7.83, 7.83, 7.83, 7.83)
    assert cell.unadjusted is not None
    assert cell.unadjusted.pct_change is None
    assert cell.unadjusted.preclose is None


def test_published_ordinary_missing_change_is_tradable_and_settles() -> None:
    reference = _ordinary_cell(_REFERENCE_DATE)
    future = _ordinary_cell(_SAMPLE_DATE, close=10.2)

    bars = OutcomeBars((reference, future))
    outcome = _evaluate(reference, future)

    assert all(bar.trading_status is OutcomeTradingStatus.TRADABLE for bar in bars)
    assert outcome.status == "complete"
    assert outcome.exit_status is OutcomeExitStatus.TRADABLE
    assert outcome.untradable_dates == ()
    assert outcome.gross_return_pct == pytest.approx(2.0)
    assert outcome.net_excess_return_pct == pytest.approx(1.8)
    assert outcome.is_retryable is False


def test_published_unknown_future_does_not_complete_return() -> None:
    outcome = _evaluate(_ordinary_cell(_REFERENCE_DATE), _observed_one_price_cell())

    assert outcome.status == "insufficient_data"
    assert outcome.quality_reason == "tradability_unknown"
    assert outcome.gross_return_pct is None
    assert outcome.net_excess_return_pct is None
    assert outcome.exit_status is None
    assert outcome.is_retryable is True


def test_published_unknown_reference_rejects_price_anchor() -> None:
    outcome = _evaluate(_observed_one_price_cell(), _ordinary_cell(date(2018, 8, 9)))

    assert outcome.status == "insufficient_data"
    assert outcome.quality_reason == "invalid_reference_bar"
    assert outcome.anchor_qfq_price is None
    assert outcome.gross_return_pct is None
    assert outcome.is_retryable is True


@pytest.mark.parametrize(
    ("one_price", "pct_change", "expected"),
    (
        (False, None, OutcomeTradingStatus.TRADABLE),
        (True, None, OutcomeTradingStatus.UNKNOWN),
        (False, float("nan"), OutcomeTradingStatus.UNKNOWN),
        (True, float("nan"), OutcomeTradingStatus.UNKNOWN),
        (False, float("inf"), OutcomeTradingStatus.UNKNOWN),
        (True, float("inf"), OutcomeTradingStatus.UNKNOWN),
        (False, float("-inf"), OutcomeTradingStatus.UNKNOWN),
        (True, float("-inf"), OutcomeTradingStatus.UNKNOWN),
        (True, -10.0, OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN),
        (True, -4.5, OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN),
        (True, -4.49, OutcomeTradingStatus.TRADABLE),
        (True, 0.0, OutcomeTradingStatus.TRADABLE),
        (True, 10.0, OutcomeTradingStatus.TRADABLE),
        (False, -10.0, OutcomeTradingStatus.TRADABLE),
    ),
)
def test_outcome_change_evidence_classification(
    one_price: bool, pct_change: float | None, expected: OutcomeTradingStatus
) -> None:
    raw = OutcomePrice(10.0, 10.0, 10.0, 10.0) if one_price else OutcomePrice(10.0, 10.5, 9.8, 10.2)
    qfq = OutcomePrice(5.0, 5.0, 5.0, 5.0) if one_price else OutcomePrice(5.0, 5.25, 4.9, 5.1)

    bar = BuildOutcomeBar(
        trade_date=_SAMPLE_DATE.isoformat(),
        qfq=qfq,
        raw=raw,
        volume=1000.0,
        pct_change=pct_change,
        source="fixture",
    )

    assert bar.trading_status is expected


@pytest.mark.parametrize("volume", (-1.0, float("nan"), float("inf"), float("-inf")))
@pytest.mark.parametrize("pct_change", (None, 0.0))
def test_outcome_invalid_volume_stays_unknown(volume: float, pct_change: float | None) -> None:
    bar = BuildOutcomeBar(
        trade_date=_SAMPLE_DATE.isoformat(),
        qfq=OutcomePrice(5.0, 5.3, 4.9, 5.1),
        raw=OutcomePrice(10.0, 10.5, 9.8, 10.2),
        volume=volume,
        pct_change=pct_change,
        source="fixture",
    )

    assert bar.trading_status is OutcomeTradingStatus.UNKNOWN


@pytest.mark.parametrize(
    ("pct_change", "expected"),
    (
        (-10.0, OutcomeTradingStatus.SUSPENDED),
        (0.0, OutcomeTradingStatus.SUSPENDED),
        (10.0, OutcomeTradingStatus.SUSPENDED),
        (None, OutcomeTradingStatus.SUSPENDED),
        (float("nan"), OutcomeTradingStatus.UNKNOWN),
        (float("inf"), OutcomeTradingStatus.UNKNOWN),
    ),
)
def test_outcome_zero_volume_keeps_suspension_classification(
    pct_change: float | None, expected: OutcomeTradingStatus,
) -> None:
    bar = BuildOutcomeBar(
        trade_date=_SAMPLE_DATE.isoformat(),
        qfq=OutcomePrice(5.0, 5.0, 5.0, 5.0),
        raw=OutcomePrice(10.0, 10.0, 10.0, 10.0),
        volume=0.0,
        pct_change=pct_change,
        source="fixture",
    )

    assert bar.trading_status is expected


def test_published_projection_uses_raw_volume_and_keeps_both_price_bases() -> None:
    cell = _ordinary_cell(_SAMPLE_DATE)
    assert cell.qfq is not None
    cell = replace(cell, qfq=replace(cell.qfq, volume=0.0))

    bars = OutcomeBars((cell,))

    assert len(bars) == 1
    assert bars[0].raw == OutcomePrice(10.0, 10.6, 9.8, 10.0)
    assert bars[0].qfq == OutcomePrice(5.0, 5.3, 4.9, 5.0)
    assert bars[0].trading_status is OutcomeTradingStatus.TRADABLE
    assert cell.unadjusted is not None and cell.qfq is not None
    assert cell.unadjusted.volume == 1000.0
    assert cell.qfq.volume == 0.0
    assert cell.unadjusted.amount == 20_000.0
    assert cell.qfq.amount == 30_000.0
    assert cell.unadjusted.pct_change is None


def test_published_outcomes_sort_reverse_input_by_trade_date() -> None:
    later = _ordinary_cell(_SAMPLE_DATE)
    earlier = _ordinary_cell(_REFERENCE_DATE)

    bars = OutcomeBars((later, earlier))

    assert tuple(bar.trade_date for bar in bars) == (
        _REFERENCE_DATE.isoformat(),
        _SAMPLE_DATE.isoformat(),
    )


@pytest.mark.parametrize("first_missing_qfq", (False, True))
def test_published_outcomes_reject_duplicate_projectable_dates(first_missing_qfq: bool) -> None:
    complete = _ordinary_cell(_SAMPLE_DATE)
    first = replace(complete, status="qfq_missing", qfq=None) if first_missing_qfq else complete

    with pytest.raises(ValueError, match="duplicate trade dates"):
        OutcomeBars((first, complete))


@pytest.mark.parametrize(("has_raw", "has_qfq"), ((False, True), (True, False), (False, False)))
def test_published_outcomes_skip_missing_sides(has_raw: bool, has_qfq: bool) -> None:
    complete = _ordinary_cell(_SAMPLE_DATE)
    if not has_raw and not has_qfq:
        incomplete = replace(complete, status="unknown_missing", unadjusted=None, qfq=None)
    elif not has_raw:
        incomplete = replace(complete, status="unadjusted_missing", unadjusted=None)
    else:
        incomplete = replace(complete, status="qfq_missing", qfq=None)

    assert OutcomeBars((incomplete,)) == ()


@pytest.mark.parametrize(
    ("side_name", "missing_field"),
    (
        ("raw", "open_price"),
        ("raw", "high_price"),
        ("raw", "low_price"),
        ("raw", "close_price"),
        ("raw", "volume"),
        ("raw", "amount"),
        ("qfq", "open_price"),
        ("qfq", "high_price"),
        ("qfq", "low_price"),
        ("qfq", "close_price"),
        ("qfq", "amount"),
    ),
)
def test_published_outcomes_skip_missing_required_reader_values(side_name: str, missing_field: str) -> None:
    complete = _ordinary_cell(_SAMPLE_DATE)
    assert complete.unadjusted is not None and complete.qfq is not None
    side = complete.unadjusted if side_name == "raw" else complete.qfq
    # Only model the reader's existing incomplete payload; ordinary fixtures validate all prices.
    incomplete_side = replace(
        side,
        open_price=None if missing_field == "open_price" else side.open_price,
        high_price=None if missing_field == "high_price" else side.high_price,
        low_price=None if missing_field == "low_price" else side.low_price,
        close_price=None if missing_field == "close_price" else side.close_price,
        volume=None if missing_field == "volume" else side.volume,
        amount=None if missing_field == "amount" else side.amount,
        already_validated=True,
    )
    incomplete = (
        replace(complete, unadjusted=incomplete_side)
        if side_name == "raw"
        else replace(complete, qfq=incomplete_side)
    )

    assert OutcomeBars((incomplete,)) == ()
