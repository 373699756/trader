"""Offline learned-rank training and pair publication contracts."""

from __future__ import annotations
from tests.helpers.training.training_resources import TrainingSampleSettingsFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
import math
from collections.abc import Iterator
from datetime import date, timedelta
import pytest
from trader.download.domain.baostock_daily import BaoStockCalendar
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.recommendation.domain.market.v1.features import LearnedRankDailyPoint
from trader.training.domain.v1.learned_rank import V1TrainingSettings
from tests.helpers.training.configuration import V1TrainingSettingsFixture
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot
from trader.training.infra.profile.v1.training import RunV1Training


class _History:
    def __init__(self) -> None:
        self.dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(340))
        self.codes = tuple(f"600{index:03d}" for index in range(12))
        self.snapshot = HistoryTrainingInputSnapshot(
            "complete_manifest",
            "a" * 64,
            "b" * 64,
            BaoStockCalendar(self.dates).content_hash,
            self.dates[-1],
            self.dates[-2],
            BaoStockCalendar(self.dates),
            self.codes,
            "d" * 64,
        )

    def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int:
        return len(allowed_dates) * len(self.codes)

    def iter_training_rows(self, allowed_dates: frozenset[date]) -> Iterator[HistoryTrainingRow]:
        for position, day in enumerate(self.dates):
            if day not in allowed_dates:
                continue
            for index, code in enumerate(self.codes):
                point = self.point(position, index)
                yield HistoryTrainingRow(
                    code,
                    day,
                    "main",
                    f"capco:industry-{index % 2}",
                    HistoryTrainingPoint(day, point.close_price, point.amount),
                    "complete",
                )

    def point(self, position: int, index: int) -> LearnedRankDailyPoint:
        return LearnedRankDailyPoint(
            self.dates[position],
            10 * math.exp((index - 5.5) * 0.0006 * position + 0.01 * math.sin(position / 7)),
            1e8 * (index + 1),
        )


def _settings() -> V1TrainingSettings:
    return V1TrainingSettingsFixture(
        fit_dates=30,
        early_stopping_dates=5,
        calibration_dates=5,
        validation_dates=5,
        minimum_training_rows=100,
        minimum_leaf_rows=5,
        boosting_rounds=12,
        early_stopping_rounds=3,
    )


@pytest.fixture(scope="module")
def reusable_pair(tmp_path_factory):
    directory = tmp_path_factory.mktemp("v1-reuse")
    result = RunV1Training(
        _History,
        directory / "models",
        history_root=directory / "history",
        sample_root=directory / "samples",
        settings=_settings(), sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(),
    )
    assert result.status == "engineering_ready" and not result.reused
    return directory / "models", result
