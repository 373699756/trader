import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.domain.research.baostock_daily import build_baostock_training_split
from trader.infra.scoring.profiles.v3.contracts import (
    D25_HEAD_CONTRACT,
    TODAY_HEAD_CONTRACT,
    TOMORROW_HEAD_CONTRACT,
)
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteV3TrainingSampleRepository,
    V3TrainingSample,
)


def _sample(
    code: str,
    day: date,
    industry: str = "银行",
    *,
    targets: tuple[float | None, ...] = (0.02, 0.03, 0.04, 0.05, 0.06, 0.045),
) -> V3TrainingSample:
    assert len(targets) == 6
    typed_targets = (targets[0], targets[1], targets[2], targets[3], targets[4], targets[5])
    return V3TrainingSample(code, day, "main", industry, 1_000_000.0, (0.1,) * 6, typed_targets)


def test_sample_repository_serves_all_three_heads_from_one_stable_database(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    training_day = split.model_fit_dates[0]
    early_day = split.early_stopping_dates[0]
    calibration_day = split.calibration_dates[0]
    validation_day = split.confirmation_dates[0]
    with SQLiteV3TrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        assert repository._connection.execute("PRAGMA cache_size").fetchone() == (-32768,)
        repository.add_final(
            (
                _sample("600002", training_day),
                _sample("600001", training_day),
                _sample("600003", early_day),
                _sample("600004", calibration_day),
                _sample("600005", validation_day),
                _sample("600006", training_day, "软件", targets=(0.02, None, None, None, None, None)),
            )
        )
        repository.prepare_for_model_fitting(split)
        repository.require_split(split)
        with pytest.raises(RuntimeError, match="identity is inconsistent"):
            repository.require_split(build_baostock_training_split(dates, parent_manifest_hash="b" * 64))
        with pytest.raises(RuntimeError, match="already prepared"):
            repository.prepare_for_model_fitting(split)

        tomorrow = next(item for item in repository.industry_counts(TOMORROW_HEAD_CONTRACT) if item.industry == "银行")
        today = next(item for item in repository.industry_counts(TODAY_HEAD_CONTRACT) if item.industry == "银行")
        d25 = next(item for item in repository.industry_counts(D25_HEAD_CONTRACT) if item.industry == "银行")
        tomorrow_data = repository.industry_data(tomorrow, TOMORROW_HEAD_CONTRACT)
        today_data = repository.industry_data(today, TODAY_HEAD_CONTRACT)
        d25_data = repository.industry_data(d25, D25_HEAD_CONTRACT)

        assert tomorrow.training == today.training == d25.training == 2
        assert (tomorrow.early_stopping, tomorrow.calibration, tomorrow.validation) == (1, 1, 1)
        assert tomorrow_data.training.features.shape == (2, 6)
        assert today_data.training.features.shape == d25_data.training.features.shape == (2, 5)
        assert tomorrow_data.training.labels.tolist() == [0.02, 0.02]
        assert d25_data.training.labels.tolist() == [0.045, 0.045]
        assert np.all(tomorrow_data.training.features == 0.1)
        assert repository.split_count("training", TOMORROW_HEAD_CONTRACT) == 3
        assert repository.split_count("training", D25_HEAD_CONTRACT) == 2
        metrics = {item.target: item for item in repository.validation_target_metrics()}
        assert metrics["target_t1"].count == 1
        assert metrics["target_d25_aggregate"].mean == pytest.approx(0.045)


def test_sample_repository_has_only_the_shared_tables_and_one_build_transaction(tmp_path: Path) -> None:
    start = date(2025, 1, 1)
    with SQLiteV3TrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        for offset in range(10):
            day = start + timedelta(days=offset)
            repository.add_final(_sample(f"{code:06d}", day) for code in range(100))

        tables = {
            str(row[0])
            for row in repository._connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        }
        assert tables == {"sample_split_dates", "samples"}
        assert repository._connection.in_transaction is True
        assert repository.count() == 1_000


def test_sample_repository_rolls_back_the_whole_workspace_build_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "samples.sqlite3"
    with pytest.raises(RuntimeError, match="forced"):
        with SQLiteV3TrainingSampleRepository(path) as repository:
            repository.add_final((_sample("600001", date(2025, 1, 1)),))
            raise RuntimeError("forced")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone() == (0,)
