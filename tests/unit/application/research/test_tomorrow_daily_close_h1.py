from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest

from trader.application.research.tomorrow_daily_close_h1 import (
    H1DailyCloseObservation,
    attach_matured_daily_close_labels,
    build_h1_daily_close_features,
)
from trader.application.research.tomorrow_daily_close_training import build_feature_dataset
from trader.domain.research.h1_point_in_time import SHANGHAI, H1PointInTimeRecord
from trader.domain.research.historical_screening import HistoricalPriceBar

_HASH = "a" * 64


def _observations(days: int = 90) -> tuple[H1DailyCloseObservation, ...]:
    first = date(2024, 1, 1)
    result = []
    for index in range(days):
        day = first + timedelta(days=index)
        for code_index, board in enumerate(("main", "chinext", "star")):
            close = 10.0 + index * (0.02 + code_index * 0.005) + code_index
            bar = HistoricalPriceBar(
                day,
                close - 0.01,
                close,
                close + 0.02,
                close - 0.02,
                1_000_000.0 + index,
                (1_000_000.0 + index) * close,
                0.2,
                1.0,
                "qfq",
                "fixture",
            )
            record = H1PointInTimeRecord(
                "tomorrow",
                f"60{code_index:04d}",
                day,
                datetime.combine(day, time(14, 50), SHANGHAI),
                bar,
                close,
                bar.volume,
                bar.amount,
                _HASH,
                _HASH,
                _HASH,
                _HASH,
            )
            result.append(H1DailyCloseObservation(record, board, True, True, _HASH))
    return tuple(result)


def test_h1_adapter_freezes_six_features_before_attaching_next_day_labels() -> None:
    observations = _observations()

    batch = build_h1_daily_close_features(observations)
    samples = attach_matured_daily_close_labels(batch, observations)
    dataset = build_feature_dataset(
        samples,
        feature_names=batch.feature_names,
        feature_units=batch.feature_units,
        source_archive_hash=batch.source_archive_hash,
        filter_spec_hash=_HASH,
    )

    assert batch.feature_names == (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    assert samples
    assert samples[0].net_excess_returns[0] - samples[0].net_excess_returns[1] == pytest.approx(0.003)
    assert samples[0].net_excess_returns[1] - samples[0].net_excess_returns[2] == pytest.approx(0.005)
    assert dataset.manifest.accepted_rows == len(samples)
    assert dataset.production_authority is False


def test_future_bar_changes_do_not_change_an_already_frozen_feature_row() -> None:
    observations = _observations()
    original = build_h1_daily_close_features(observations)
    target = original.rows[0]
    last = observations[-1]
    changed_bar = replace(
        last.record.daily_bar,
        close=last.record.daily_bar.close + 5.0,
        high=last.record.daily_bar.high + 5.0,
    )
    changed_record = replace(last.record, daily_bar=changed_bar, anchor_price=changed_bar.close)
    changed = build_h1_daily_close_features((*observations[:-1], replace(last, record=changed_record)))
    replayed_target = next(
        row for row in changed.rows if row.trade_date == target.trade_date and row.code == target.code
    )

    assert replayed_target == target
    assert replayed_target.content_hash == target.content_hash


def test_incomplete_point_in_time_filter_evidence_never_enters_training_dataset() -> None:
    observations = list(_observations())
    target_index = next(index for index, item in enumerate(observations) if item.record.trade_date >= date(2024, 3, 6))
    observations[target_index] = replace(
        observations[target_index], hard_filter_evidence_complete=False, filter_evidence_hash=None
    )
    batch = build_h1_daily_close_features(tuple(observations))
    samples = attach_matured_daily_close_labels(batch, tuple(observations))
    dataset = build_feature_dataset(
        samples,
        feature_names=batch.feature_names,
        feature_units=batch.feature_units,
        source_archive_hash=batch.source_archive_hash,
        filter_spec_hash=_HASH,
    )

    assert dataset.manifest.rejected_filter_evidence_rows == 1
