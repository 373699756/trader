"""Training liquidity uses the same completed sessions as online scoring."""

from collections import deque
from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.market.history_metrics import DailyBar, PriceAdjustment
from trader.recommendation.domain.market.v5.features import PriceVolumePoint
from trader.recommendation.infra.normalization.factor_history import BuildFactorHistory
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.research.close_return.v2_contracts import V2_TRAINING_PROFILE
from trader.training.infra.research.close_return.v3_contracts import V3_TRAINING_PROFILE
from trader.training.infra.research.close_return.v5_contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_builder import _pending_sample, _training_window_sessions


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE, V5_TRAINING_PROFILE))
def test_training_amount_excludes_signal_and_keeps_signal_price(profile: TrainedProfileContract) -> None:
    days = tuple(date(2025, 1, 1) + timedelta(days=i) for i in range(_training_window_sessions(profile)))
    points = tuple(
        HistoryTrainingPoint(day, 100.0 + i, float(i + 1), 100.0 + i, 100.0 + i, 100.0 + i, 10.0)
        for i, day in enumerate(days)
    )
    points = (*points[:-1], replace(points[-1], qfq_amount=1_000_000.0))
    row = HistoryTrainingRow("600001", days[-1], "main", "capco:制造业", points[-1], "complete")
    sequence = (
        deque(
            PriceVolumePoint(
                point.trade_date, point.qfq_open, point.qfq_high, point.qfq_low,
                point.qfq_close_price, point.raw_volume, point.qfq_amount,
            )
            for point in points
        )
        if profile.algorithm == "tcn" else None
    )
    sample = _pending_sample(row, deque(points), len(days) - 1, frozenset(days), profile, sequence)
    bars = tuple(
        DailyBar(
            trade_date=point.trade_date.isoformat(), open_price=point.qfq_open,
            close=point.qfq_close_price, high=point.qfq_high, low=point.qfq_low,
            volume=point.raw_volume, amount=point.qfq_amount, pct_change=0.0,
            turnover_rate=None, adjustment=PriceAdjustment.QFQ, source="fixture",
        )
        for point in points
    )
    online = BuildFactorHistory(bars, None, days[-1].isoformat())
    assert sample is not None
    assert sample.trade_date == days[-1]
    assert sample.close == points[-1].qfq_close_price
    assert sample.average_amount_20d == pytest.approx(sum(range(len(days) - 20, len(days))) / 20.0)
    assert sample.average_amount_20d == online.average_amount_20d
    return_index = profile.raw_feature_manifest.names.index("qfq_return_1d")
    assert sample.features[return_index] == pytest.approx(points[-1].qfq_close_price / points[-2].qfq_close_price - 1.0)


@pytest.mark.parametrize("fault", ("missing_oldest_completed", "zero_completed_window", "zero_signal"))
def test_incomplete_completed_amount_window_and_nontrading_signal_are_rejected(fault: str) -> None:
    days = tuple(date(2025, 1, 1) + timedelta(days=i) for i in range(V3_TRAINING_PROFILE.history_sessions))
    points = [HistoryTrainingPoint(day, 100.0 + i, 1.0) for i, day in enumerate(days)]
    if fault == "missing_oldest_completed":
        points[-21] = replace(points[-21], qfq_amount=None)
    elif fault == "zero_completed_window":
        points[-21:-1] = [replace(point, qfq_amount=0.0) for point in points[-21:-1]]
    else:
        points[-1] = replace(points[-1], qfq_amount=0.0)
    row = HistoryTrainingRow("600001", days[-1], "main", "capco:制造业", points[-1], "complete")
    assert _pending_sample(
        row, deque(points), len(days) - 1, frozenset(days), V3_TRAINING_PROFILE, None,
    ) is None
