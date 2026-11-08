"""Cached V5 points preserve the shared sequence formula and invalid-window rules."""

from tests.helpers.training.training_resources import TrainingSampleSettingsFixture

from dataclasses import dataclass, replace
from datetime import date, timedelta

import numpy as np
import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import TomorrowTrainingWindow
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra import sample_builder
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_builder import BuildTrainingSamples, TrainingSampleBuildRequest
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex
from trader.training.infra.sample_sequences import SampleSequenceLayout


@dataclass
class _History:
    snapshot: HistoryTrainingInputSnapshot

    def training_row_upper_bound(self, allowed_dates):
        return len(allowed_dates)

    def has_industry_window(self, *args, **kwargs):
        raise AssertionError("sample construction must not invoke fitting admission")

    def iter_training_rows(self, *args, **kwargs):
        raise AssertionError("an injected history stream must not trigger another scan")


@pytest.mark.parametrize("fault", (
    "complete", "missing_open", "missing_volume", "invalid_ohlc", "zero_volume", "zero_amount",
    "missing_point", "gap", "suspended", "estimated",
))
def test_cached_points_match_formula_and_are_validated_once_per_source_row(tmp_path, monkeypatch, fault):
    calendar = tuple(date(2018, 1, 1) + timedelta(days=offset) for offset in range(2000))
    split = BuildTrainingDateSplit(
        calendar,
        parent_manifest_hash="a" * 64,
        fit_dates=61,
        history_sessions=61,
        recent_fit=True,
        history_excludes_signal=True,
    )
    window = TomorrowTrainingWindow(split)
    fault_day = split.model_fit_dates[5]
    rows = []
    for number, day in enumerate(calendar):
        if day not in window.readable_dates or day == fault_day and fault == "gap":
            continue
        close = 10.0 + number / 100
        point = HistoryTrainingPoint(
            day,
            close,
            1_000_000.0 + number,
            close - 0.1,
            close + 0.2,
            close - 0.2,
            1_000.0 + number,
        )
        if day == fault_day:
            if fault == "missing_open":
                point = replace(point, qfq_open=None)
            elif fault == "missing_volume":
                point = replace(point, raw_volume=None)
            elif fault == "invalid_ohlc":
                point = replace(point, qfq_high=close - 0.3)
            elif fault == "zero_volume":
                point = replace(point, raw_volume=0.0)
            elif fault == "zero_amount":
                point = replace(point, qfq_amount=0.0)
            elif fault in {"suspended", "estimated"}:
                point = replace(
                    point,
                    qfq_open=close,
                    qfq_high=close,
                    qfq_low=close,
                    raw_volume=0.0,
                    qfq_amount=0.0,
                )
        rows.append(HistoryTrainingRow(
            "600001",
            day,
            "main",
            "fixture",
            None if day == fault_day and fault == "missing_point" else point,
            "supplier_marked_suspended" if day == fault_day and fault == "suspended" else
            "unknown_missing" if day == fault_day and fault == "estimated" else "complete",
        ))
    source = _History(HistoryTrainingInputSnapshot(
        "complete_manifest",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        calendar[-1],
        calendar[-6],
        BaoStockCalendar(calendar),
        ("600001",),
        "d" * 64,
        industry_snapshot=CurrentIndustrySnapshotFixture(("600001",)),
    ))
    expected = {}
    row_by_day = {row.trade_date: row for row in rows}
    by_day = {row.trade_date: row.point for row in rows}
    for day in split.model_fit_dates:
        position = calendar.index(day)
        dates = calendar[position - 61:position]
        points = tuple(by_day.get(previous) for previous in dates)
        if (
            by_day.get(day) is None
            or by_day[day].qfq_amount == 0
            or row_by_day[day].cell_status != "complete"
            or any(point is None for point in points)
        ):
            continue
        try:
            expected[day] = CalculatePriceVolumeSequence(
                "600001",
                tuple(PriceVolumePoint(
                    point.trade_date,
                    point.qfq_open,
                    point.qfq_high,
                    point.qfq_low,
                    point.qfq_close_price,
                    point.raw_volume,
                    point.qfq_amount,
                ) for point in points),
                expected_dates=dates,
                source_identity="v5",
            ).values
        except (ValueError, TypeError):
            continue
    calls = []

    def counted_point(*args):
        calls.append(args[0])
        return PriceVolumePoint(*args)

    monkeypatch.setattr(sample_builder, "PriceVolumePoint", counted_point)
    with SQLiteTrainingSampleIndex(
        tmp_path / "samples.sqlite3",
        V5_TRAINING_PROFILE.heads[0].feature_manifest.names,
        sequence_layout=SampleSequenceLayout(6, 60, 6),
        sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(),
    ) as samples:
        BuildTrainingSamples(
            TrainingSampleBuildRequest(
                source,
                ("600001",),
                window,
                samples,
                V5_TRAINING_PROFILE,
            ),
            aligned_rows=iter(rows),
        )
        for head in V5_TRAINING_PROFILE.heads:
            horizons = (1,) if head.strategy is Strategy.TOMORROW else (2, 3, 4, 5)
            expected_head = {
                day: values
                for day, values in expected.items()
                if all(
                    by_day.get(future := calendar[calendar.index(day) + horizon]) is not None
                    and row_by_day[future].cell_status in {"complete", "supplier_marked_suspended"}
                    for horizon in horizons
                )
            }
            actual = {}
            for batch in samples.iter_batches("fixture", head, "training"):
                for day, sequence in zip(batch.trade_dates, batch.features[:, -360:], strict=True):
                    actual[day] = sequence
                np.testing.assert_array_equal(batch.labels, np.zeros(len(batch.labels)))
            assert actual.keys() == expected_head.keys()
            for day, values in actual.items():
                np.testing.assert_array_equal(values, expected_head[day])
    expected_calls = sum(
        row.point is not None and all(value is not None for value in (
            row.point.qfq_open, row.point.qfq_high, row.point.qfq_low,
            row.point.qfq_close_price, row.point.raw_volume, row.point.qfq_amount,
        ))
        for row in rows
    )
    assert len(calls) == expected_calls
