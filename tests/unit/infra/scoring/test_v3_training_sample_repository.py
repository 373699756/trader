import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.domain.research.baostock_daily import build_baostock_training_split
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteTomorrowTrainingSampleRepository,
    TomorrowTrainingSample,
)


def _sample(code: str, day: date, industry: str = "银行") -> TomorrowTrainingSample:
    return TomorrowTrainingSample(code, day, "main", industry, 1_000_000.0, (0.1,) * 6, 0.02)


def test_sample_repository_streams_by_day_and_industry_in_stable_order(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_250))
    split = build_baostock_training_split(dates, parent_manifest_hash="a" * 64)
    training_day = split.model_fit_dates[0]
    early_day = split.early_stopping_dates[0]
    calibration_day = split.calibration_dates[0]
    validation_day = split.confirmation_dates[0]
    with SQLiteTomorrowTrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
        assert repository._connection.execute("PRAGMA cache_size").fetchone() == (-32768,)
        repository.add_final(
            (
                _sample("600002", training_day),
                _sample("600001", training_day),
                _sample("600003", early_day),
                _sample("600004", calibration_day),
                _sample("600005", validation_day),
                _sample("600006", training_day, "软件"),
            )
        )
        repository.prepare_for_model_fitting(split)
        repository.require_split(split)
        mismatched_split = build_baostock_training_split(dates, parent_manifest_hash="b" * 64)
        with pytest.raises(RuntimeError, match="identity is inconsistent"):
            repository.require_split(mismatched_split)
        with pytest.raises(RuntimeError, match="already prepared"):
            repository.prepare_for_model_fitting(split)
        trace: list[str] = []
        repository._connection.set_trace_callback(trace.append)

        counts = repository.industry_counts()
        bank = next(item for item in counts if item.industry == "银行")
        data = repository.industry_data(bank)

        assert bank.training == 2
        assert (bank.early_stopping, bank.calibration, bank.validation) == (1, 1, 1)
        assert data.training.features.shape == (2, 6)
        assert data.training.labels.tolist() == [0.02, 0.02]
        assert np.all(data.training.features == 0.1)
        assert data.validation_count == 1
        assert repository.split_count("training") == 3
        selects = tuple(statement for statement in trace if statement.lstrip().upper().startswith("SELECT"))
        assert len(selects) == 3  # one aggregate, one industry scan, one total


def test_sample_repository_has_only_the_final_table_and_one_build_transaction(tmp_path: Path) -> None:
    start = date(2025, 1, 1)
    with SQLiteTomorrowTrainingSampleRepository(tmp_path / "samples.sqlite3") as repository:
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
        with SQLiteTomorrowTrainingSampleRepository(path) as repository:
            repository.add_final((_sample("600001", date(2025, 1, 1)),))
            raise RuntimeError("forced")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM samples").fetchone() == (0,)
