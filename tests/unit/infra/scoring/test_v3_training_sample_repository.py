from datetime import date, timedelta
from pathlib import Path

import numpy as np

from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteTomorrowTrainingSampleRepository,
    TomorrowTrainingSample,
)


def _sample(code: str, day: date, industry: str = "银行") -> TomorrowTrainingSample:
    return TomorrowTrainingSample(code, day, "main", industry, 1_000_000.0, (0.1,) * 6, 0.02)


def test_sample_repository_streams_by_day_and_industry_in_stable_order(tmp_path: Path) -> None:
    first = date(2026, 1, 2)
    second = first + timedelta(days=1)
    with SQLiteTomorrowTrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        repository.add_raw((_sample("600002", first), _sample("600001", first), _sample("600003", second)))

        assert repository.raw_dates() == (first, second)
        assert tuple(item.code for item in repository.raw_for_date(first)) == ("600001", "600002")

        repository.add_final(repository.raw_for_date(first))
        repository.add_final((_sample("600003", second, "软件"),))
        repository.prepare_for_model_fitting()

        assert repository.count() == 3
        assert repository.industries(frozenset((first, second))) == ("软件", "银行")
        assert tuple(item.code for item in repository.samples_for("银行", frozenset((first,)))) == (
            "600001",
            "600002",
        )

        matrix = repository.matrix_for("银行", frozenset((first,)))
        assert matrix.features.shape == (2, 6)
        assert matrix.labels.tolist() == [0.02, 0.02]
        assert np.all(matrix.features == 0.1)


def test_sample_repository_keeps_large_population_out_of_one_python_collection(tmp_path: Path) -> None:
    start = date(2025, 1, 1)
    with SQLiteTomorrowTrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        for offset in range(100):
            day = start + timedelta(days=offset)
            repository.add_raw(_sample(f"{code:06d}", day) for code in range(1_000))

        assert repository.count() == 0
        assert len(repository.raw_for_date(start)) == 1_000
        assert len(repository.raw_dates()) == 100
