from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.market.history_alignment import AlignMarketSession, MarketDailyObservation
from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint
from trader.training.domain.label_benchmark import ObservedTrainingBenchmark
from trader.training.infra.history.aligned_training_rows import AlignTrainingRows
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.sample_builder import _finalize_sample_day, _PendingSample


def test_observed_training_benchmark_never_fills_missing_or_accepts_invalid_returns():
    assert ObservedTrainingBenchmark((None, None)) is None
    assert ObservedTrainingBenchmark((0.2, None, -0.1)) == pytest.approx(0.05)
    with pytest.raises(ValueError, match="non_finite"):
        ObservedTrainingBenchmark((0.1, float("nan")))


def test_forward_valuation_preserves_dates_and_never_promotes_estimates_to_labels():
    first = date(2026, 10, 19)
    observed = AlignMarketSession(first, MarketDailyObservation(first, 10.0, 100.0), None)
    missing = AlignMarketSession(first + timedelta(days=1), None, observed)
    assert missing is not None
    assert (missing.close, missing.amount, missing.kind, missing.label_available) == (10.0, 0.0, "estimated", False)
    suspended = AlignMarketSession(
        first + timedelta(days=2),
        MarketDailyObservation(first + timedelta(days=2), None, None, kind="suspended"),
        missing,
    )
    assert suspended is not None and suspended.close == 10 and not suspended.label_available
    confirmed = AlignMarketSession(
        first + timedelta(days=2),
        MarketDailyObservation(first + timedelta(days=2), 10.0, 0.0, kind="suspended"),
        missing,
    )
    assert confirmed is not None and confirmed.label_available
    unconfirmed = AlignMarketSession(
        first + timedelta(days=1),
        MarketDailyObservation(first + timedelta(days=1), 99.0, 500.0, kind="estimated"),
        observed,
    )
    assert unconfirmed is not None and unconfirmed.close == 10 and not unconfirmed.label_available
    assert AlignMarketSession(first, None, None) is None
    assert (
        AlignMarketSession(
            first + timedelta(days=1), MarketDailyObservation(first + timedelta(days=1), -1.0, 1.0), observed
        )
        is None
    )


def test_training_alignment_does_not_compress_missing_stock_sessions():
    days = tuple(date(2026, 10, 19) + timedelta(days=n) for n in range(3))
    rows = tuple(
        HistoryTrainingRow(code, day, "main", "industry", HistoryTrainingPoint(day, 10.0 + n, 100.0), "complete")
        for n, day in enumerate(days)
        for code in ("600001", "600002")
        if not (n == 1 and code == "600001")
    )
    aligned = tuple(AlignTrainingRows(rows, days))
    stock = tuple(row for row in aligned if row.code == "600001")
    assert tuple(row.trade_date for row in stock) == days
    assert stock[1].cell_status == "unknown_missing"
    assert stock[1].point.qfq_close_price == 10
    assert stock[1].point.qfq_amount == 0
    assert stock[2].point.qfq_close_price == 12
    with pytest.raises(ValueError, match="source_day_missing"):
        tuple(AlignTrainingRows((rows[0], rows[-1]), days))


def test_v5_zero_flow_session_has_a_real_position_in_the_sequence():
    days = tuple(date(2026, 1, 1) + timedelta(days=n) for n in range(61))
    points = tuple(
        PriceVolumePoint(day, 10, 10, 10, 10, 0 if n == 20 else 100, 0 if n == 20 else 1000)
        for n, day in enumerate(days)
    )
    sequence = CalculatePriceVolumeSequence("600001", points, expected_dates=days, source_identity="fixture")
    assert sequence.values[19 * 6 : 20 * 6] == (0, 0, 0, 0, 0, 0)
    assert len(sequence.values) == 360


def test_d25_survives_unavailable_t1_and_other_stocks_keep_observed_targets():
    first = _PendingSample(
        "600001",
        date(2026, 10, 19),
        0,
        "main",
        "industry",
        1000,
        (0.1, 0.1, 0.1, 0.2, 0.3, 0.4),
        10,
        [None, 0.2, 0.3, 0.4, 0.5],
    )
    second = replace(first, code="600002", future_returns=[0.1, 0.1, 0.2, 0.3, 0.4])
    samples = _finalize_sample_day((first, second), V3_TRAINING_PROFILE)
    assert len(samples) == 2
    assert samples[0].targets[0] is None
    assert samples[1].targets[0] == 0.0
    assert samples[0].targets[-1] == pytest.approx(0.05)
    assert samples[1].targets[-1] == pytest.approx(-0.05)


def test_horizon_benchmarks_use_observed_returns_without_changing_feature_exposures():
    first = _PendingSample(
        "600001",
        date(2026, 10, 19),
        0,
        "main",
        "industry",
        1000,
        (0.1, 0.1, 0.1, 0.2, 0.3, 0.4),
        10,
        [0.1, 0.2, 0.3, 0.4, 0.5],
    )
    second = replace(first, code="600002", future_returns=[0.2, None, 0.4, 0.5, 0.6])
    third = replace(first, code="600003", future_returns=[None, 0.4, 0.5, 0.6, 0.7])
    samples = _finalize_sample_day((first, second, third), V3_TRAINING_PROFILE)
    assert len(samples) == 3
    assert samples[0].targets == pytest.approx((-0.05, -0.1, -0.1, -0.1, -0.1, -0.1))
    assert samples[1].targets[0] == pytest.approx(0.05)
    assert samples[1].targets[1] is None and samples[1].targets[-1] is None
    assert samples[2].targets[0] is None and samples[2].targets[-1] == pytest.approx(0.1)
    complete = _finalize_sample_day(
        (
            first,
            replace(second, future_returns=first.future_returns),
            replace(third, future_returns=first.future_returns),
        ),
        V3_TRAINING_PROFILE,
    )
    assert tuple(sample.features for sample in samples) == tuple(sample.features for sample in complete)
