from dataclasses import replace
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.profile.v3.contracts import (
    D25_HEAD_CONTRACT,
    TOMORROW_HEAD_CONTRACT,
)
from trader.training.infra.profile.v3.training_sample_index import (
    SQLiteV3TrainingSampleIndex,
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


def test_shared_industry_reads_once_and_keeps_head_matrices_independent(tmp_path):
    split = BuildTrainingDateSplit(tuple(date(2021, 1, 1) + timedelta(days=i) for i in range(1600)), parent_manifest_hash="a" * 64)
    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as index:
        index.add_final((_sample("600001", split.model_fit_dates[0]), _sample("600002", split.model_fit_dates[0], targets=(None, 0.03, 0.04, 0.05, 0.06, 0.045))))
        index.prepare_for_model_fitting(split)
        first = index.industry_counts(TOMORROW_HEAD_CONTRACT)[0]
        second = index.industry_counts(D25_HEAD_CONTRACT)[0]
        statements = []
        index._connection.observe_statements(statements.append)
        with index.share_industry("银行"):
            with index.industry_data(first, TOMORROW_HEAD_CONTRACT) as data:
                assert len(data.training.labels) == 1
                data.training.features[:] = 99
            with index.industry_data(second, D25_HEAD_CONTRACT) as data:
                assert len(data.training.labels) == 2
                assert np.all(data.training.features == 0.1)
        assert sum("SELECT d.split_name,s.trade_date,s.code" in sql for sql in statements) == 1
        assert not any("SELECT s.trade_date,s.code" in sql for sql in statements)
        assert not tuple(tmp_path.glob("sample-shared-*"))


@pytest.mark.parametrize("failure", (None, RuntimeError, InterruptedError))
def test_large_matrices_release_mappings_and_files_on_every_exit(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("trader.training.infra.sample_index.TRAINING_MATRIX_BYTES", 1)
    split = BuildTrainingDateSplit(
        tuple(date(2021, 1, 1) + timedelta(days=i) for i in range(1600)), parent_manifest_hash="a" * 64
    )
    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as index:
        index.add_final((_sample("600001", split.model_fit_dates[0]),))
        index.prepare_for_model_fitting(split)
        counts = index.industry_counts(TOMORROW_HEAD_CONTRACT)[0]
        buffers = []
        try:
            with index.industry_data(counts, TOMORROW_HEAD_CONTRACT) as data:
                buffers.extend((data.training.features.base, data.training.labels.base))
                assert data.training.features.flags.c_contiguous
                assert data.training.features.tolist() == [[0.1] * 6]
                assert tuple(tmp_path.glob("sample-fit-*"))
                if failure is not None:
                    raise failure("fixture exit")
        except (RuntimeError, InterruptedError) as exc:
            assert failure is not None and type(exc) is failure
        assert all(buffer.closed for buffer in buffers)
        assert not tuple(tmp_path.glob("sample-fit-*"))


def test_sample_index_serves_both_heads_from_one_stable_database(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    training_day = split.model_fit_dates[0]
    early_day = split.early_stopping_dates[0]
    calibration_day = split.calibration_dates[0]
    validation_day = split.confirmation_dates[0]
    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as repository, ExitStack() as resources:
        assert tuple(repository._connection.run("PRAGMA cache_size").fetchone()) == (-32768,)
        assert repository.industry_counts(TOMORROW_HEAD_CONTRACT) == ()
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
        assert repository.industry_counts(TOMORROW_HEAD_CONTRACT) == ()
        repository.prepare_for_model_fitting(split)
        repository.require_split(split)
        with pytest.raises(RuntimeError, match="identity is inconsistent"):
            repository.require_split(BuildTrainingDateSplit(dates, parent_manifest_hash="b" * 64))
        with pytest.raises(RuntimeError, match="already prepared"):
            repository.prepare_for_model_fitting(split)

        statements = []
        repository._connection.observe_statements(statements.append)
        tomorrow = next(item for item in repository.industry_counts(TOMORROW_HEAD_CONTRACT) if item.industry == "银行")
        d25 = next(item for item in repository.industry_counts(D25_HEAD_CONTRACT) if item.industry == "银行")
        repository._connection.observe_statements(None)
        assert len(statements) == 1 and "FROM sample_statistics samples" in statements[0]
        tomorrow_data = resources.enter_context(repository.industry_data(tomorrow, TOMORROW_HEAD_CONTRACT))
        d25_data = resources.enter_context(repository.industry_data(d25, D25_HEAD_CONTRACT))

        assert tomorrow.training == d25.training == 2
        assert (tomorrow.early_stopping, tomorrow.calibration, tomorrow.validation) == (1, 1, 1)
        assert tomorrow_data.training.features.shape == (2, 6)
        assert d25_data.training.features.shape == (2, 5)
        assert tomorrow_data.training.labels.tolist() == [0.02, 0.02]
        assert d25_data.training.labels.tolist() == [0.045, 0.045]
        assert np.all(tomorrow_data.training.features == 0.1)
        assert repository.split_count("training", TOMORROW_HEAD_CONTRACT) == 3
        assert repository.split_count("training", D25_HEAD_CONTRACT) == 2
        assert repository.split_distinct_date_count("training", TOMORROW_HEAD_CONTRACT) == 1
        assert repository.split_distinct_date_count("validation", TOMORROW_HEAD_CONTRACT) == 1
        metrics = {item.target: item for item in repository.validation_target_metrics()}
        assert metrics["target_t1"].count == 1
        assert metrics["target_d25_aggregate"].mean == pytest.approx(0.045)


def test_sample_index_has_only_the_shared_tables_and_one_build_transaction(tmp_path: Path) -> None:
    start = date(2025, 1, 1)
    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as repository:
        for offset in range(10):
            day = start + timedelta(days=offset)
            repository.add_final(_sample(f"{code:06d}", day) for code in range(100))

        tables = {
            str(row[0])
            for row in repository._connection.run("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        }
        assert tables == {"sample_contract", "sample_split_dates", "samples", "sample_statistics", "sample_day_diagnostics"}
        assert repository._connection.in_transaction is True
        assert repository.count() == 1_000


@pytest.mark.parametrize("contract,target_position", ((TOMORROW_HEAD_CONTRACT, 0), (D25_HEAD_CONTRACT, 5)))
def test_industry_matrices_preserve_order_and_split_boundaries_across_read_batches(tmp_path, contract, target_position):
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    sample_dates = (
        (split.model_fit_dates[0],) * 4_097
        + (split.early_stopping_dates[0],) * 2
        + (split.calibration_dates[0],) * 2
        + (split.calibration_dates[1],) * 3
    )
    samples = tuple(
        replace(
            _sample(f"{600000 + number:06d}", day, targets=(number / 10, 0.03, 0.04, 0.05, 0.06, number / 20)),
            features=tuple(float(number * 10 + position) for position in range(6)),
        )
        for number, day in enumerate(sample_dates)
    ) + (_sample("650000", split.model_fit_dates[0], targets=(0.9, None, None, None, None, None)),)
    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as repository, ExitStack() as resources:
        repository.add_final(reversed(samples))
        repository.prepare_for_model_fitting(split)
        counts = repository.industry_counts(contract)[0]
        data = resources.enter_context(repository.industry_data(counts, contract))
        for selected_dates, matrix in (
            (split.model_fit_dates, data.training),
            (split.early_stopping_dates, data.early_stopping),
            (split.calibration_dates, data.calibration),
        ):
            expected = sorted(
                (
                    sample
                    for sample in samples
                    if sample.trade_date in selected_dates and sample.targets[target_position] is not None
                ),
                key=lambda sample: (sample.trade_date, sample.code),
            )
            np.testing.assert_array_equal(
                matrix.features,
                [[sample.features[position] for position in contract.feature_positions] for sample in expected],
            )
            np.testing.assert_array_equal(matrix.labels, [sample.targets[target_position] for sample in expected])
        assert data.calibration_dates == (split.calibration_dates[0],) * 2 + (split.calibration_dates[1],) * 3
        for delta in (-1, 1):
            with pytest.raises(RuntimeError, match="split count changed"):
                with repository.industry_data(replace(counts, training=counts.training + delta), contract):
                    pass


def test_validation_statistics_use_one_scan_and_include_validation_only_industries(tmp_path: Path) -> None:
    dates = tuple(date(2021, 1, 1) + timedelta(days=index) for index in range(1_600))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    with SQLiteV3TrainingSampleIndex(tmp_path / "samples.sqlite3") as repository:
        repository.add_final(
            (
                _sample("600001", split.model_fit_dates[0]),
                _sample("600002", split.confirmation_dates[0], targets=(0.02, None, None, None, None, None)),
                _sample("600003", split.confirmation_dates[0], "软件", targets=(0.04, 0.03, None, None, None, 0.045)),
            )
        )
        repository.prepare_for_model_fitting(split)
        statements: list[str] = []
        repository._connection.observe_statements(statements.append)
        metrics = {item.target: item for item in repository.validation_target_metrics()}
        repository._connection.observe_statements(None)

        assert len(statements) == 1
        assert metrics["target_t1"].count == 2
        assert metrics["target_t1"].mean == pytest.approx(0.03)
        assert metrics["target_t1"].standard_deviation == pytest.approx(0.01)
        assert metrics["target_t2"].count == 1
        assert metrics["target_t2"].standard_deviation == 0.0
        assert metrics["target_t3"].count == 0
        assert metrics["target_t3"].mean is None
        assert metrics["target_t3"].standard_deviation is None
        counts = repository.industry_counts(TOMORROW_HEAD_CONTRACT)
        software = next(item for item in counts if item.industry == "软件")
        assert (software.training, software.validation) == (0, 1)
        assert sum(item.validation for item in counts) == repository.split_count("validation", TOMORROW_HEAD_CONTRACT)


def test_sample_index_rolls_back_the_whole_workspace_build_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "samples.sqlite3"
    with pytest.raises(RuntimeError, match="forced"):
        with SQLiteV3TrainingSampleIndex(path) as repository:
            repository.add_final((_sample("600001", date(2025, 1, 1)),))
            raise RuntimeError("forced")

    with SqliteSession(path) as connection, SqliteTransaction(connection):
        assert tuple(connection.run("SELECT COUNT(*) FROM samples").fetchone()) == (0,)
