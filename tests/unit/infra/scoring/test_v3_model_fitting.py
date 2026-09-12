from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from trader.application.research.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS
from trader.domain.research.baostock_daily import build_baostock_training_split
from trader.infra.scoring.profiles.v3.model_fitting import fit_industry_models
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteTomorrowTrainingSampleRepository,
    TomorrowTrainingSample,
)


def test_industry_fitting_is_deterministic_with_the_three_thread_limit(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    usable_dates = tuple((*split.development_dates, *split.confirmation_dates, *split.daily_proxy_holdout_dates))

    with SQLiteTomorrowTrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        for day_position, day in enumerate(usable_dates):
            repository.add_final(_samples_for_day(day, day_position))
        repository.prepare_for_model_fitting()

        first, first_training_rows, first_validation_rows = fit_industry_models(repository, split)
        second, second_training_rows, second_validation_rows = fit_industry_models(repository, split)

    assert TOMORROW_TRAINING_COMPUTE_THREADS == 3
    assert first == second
    assert first_training_rows == second_training_rows
    assert first_validation_rows == second_validation_rows
    assert "[num_threads: 3]" in str(first["银行"]["lightgbm_model"])


def _samples_for_day(day: date, day_position: int) -> tuple[TomorrowTrainingSample, ...]:
    return tuple(
        TomorrowTrainingSample(
            f"{code:06d}",
            day,
            "main",
            "银行",
            1_000_000.0 + code,
            tuple((day_position % (offset + 11) + code % (offset + 5)) / 100.0 for offset in range(6)),
            ((day_position % 17) - (code % 7)) / 1_000.0,
        )
        for code in range(1, 36)
    )
