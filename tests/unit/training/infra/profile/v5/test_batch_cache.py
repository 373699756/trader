"""Float64 batch identity and lifetime for memory and file-backed TCN caches."""

from tests.helpers.training_resources import TrainingSampleSettingsFixture

from tests.helpers.configuration import SqlitePolicyFixture

from datetime import date, timedelta
from multiprocessing import get_context
from pathlib import Path
import os

import numpy as np
import pytest

from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.training.infra.profile.v5 import batch_cache
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample


@pytest.mark.parametrize("limit", (1, 256 * 1024**2))
def test_cache_reads_two_partitions_once_and_preserves_batches(tmp_path, monkeypatch, limit):
    contract = V5_TRAINING_PROFILE.heads[0]
    width = max(contract.feature_positions) + 1
    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    split = BuildTrainingDateSplit(
        dates,
        parent_manifest_hash="a" * 64,
        fit_dates=750,
        history_sessions=61,
        history_excludes_signal=True,
    )
    with SQLiteTrainingSampleIndex(tmp_path / "samples.sqlite3", contract.feature_manifest.names, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture(tcn_batch_memory_bytes=limit)) as samples:
        samples.add_final(
            TrainingSample(f"{i:06d}", day, "main", "bank", 1e8, tuple(float(j + i) for j in range(width)), (0.01,) * 6)
            for day in (split.model_fit_dates[0], split.early_stopping_dates[0])
            for i in range(5)
        )
        samples.prepare_for_model_fitting(split)
        counts = samples.industry_counts(contract)[0]
        expected = tuple(samples.iter_batches("bank", contract, "training", batch_size=2))
        statements = []
        samples._connection.observe_statements(statements.append)
        with batch_cache.CacheTcnBatches(samples, counts, contract, 2, lambda: None) as cache:
            for _epoch in range(3):
                for actual, original in zip(cache.batches("training"), expected, strict=True):
                    assert actual.features.dtype == np.float64
                    np.testing.assert_array_equal(actual.features, original.features)
                    np.testing.assert_array_equal(actual.labels, original.labels)
            assert bool(tuple(tmp_path.glob("tcn-batches-*"))) == (limit == 1)
            buffers = (cache.training.base, cache.early_stopping.base) if limit == 1 else ()
        assert len(statements) == 2
        assert not tuple(tmp_path.glob("tcn-batches-*"))
        assert all(buffer.closed for buffer in buffers)

        def cancelled():
            raise InterruptedError("cancelled")

        with pytest.raises(InterruptedError), batch_cache.CacheTcnBatches(samples, counts, contract, 2, cancelled):
            pytest.fail("cancelled cache construction completed")
        assert not tuple(tmp_path.glob("tcn-batches-*"))

        with pytest.raises(InterruptedError):
            with batch_cache.CacheTcnBatches(samples, counts, contract, 2, lambda: None) as cache:
                buffers = (cache.training.base, cache.early_stopping.base) if limit == 1 else ()
                raise InterruptedError("cancelled while fitting")
        assert all(buffer.closed for buffer in buffers)
        assert not tuple(tmp_path.glob("tcn-batches-*"))


def _restart_cache(root: Path) -> TrainingSampleCache:
    contract = V5_TRAINING_PROFILE.heads[0]
    split = BuildTrainingDateSplit(
        tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000)),
        parent_manifest_hash="a" * 64,
        fit_dates=750,
        history_sessions=61,
        history_excludes_signal=True,
    )
    return TrainingSampleCache(
        root,
        "v5-hard-exit-fixture",
        contract.feature_manifest.names,
        split.content_hash,
        sqlite_policy=SqlitePolicyFixture(),
        sample_settings=TrainingSampleSettingsFixture(tcn_batch_memory_bytes=1),
    )


def _crash_tcn_cache(root: Path, point: str) -> None:
    contract = V5_TRAINING_PROFILE.heads[0]

    def checkpoint() -> None:
        if point == "filling":
            os._exit(86)

    with HistoryMaintenanceLock(root.parent.parent / "models/.v5.training.lock"):
        with _restart_cache(root).open() as samples:
            counts = samples.industry_counts(contract)[0]
            with batch_cache.CacheTcnBatches(samples, counts, contract, 2, checkpoint):
                os._exit(86)


@pytest.mark.parametrize("point", ("filling", "ready"))
def test_hard_exit_releases_lock_and_reopens_sealed_samples_without_batch_residue(tmp_path, point):
    root = tmp_path / "samples/v5"
    contract = V5_TRAINING_PROFILE.heads[0]
    width = max(contract.feature_positions) + 1
    split = BuildTrainingDateSplit(
        tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000)),
        parent_manifest_hash="a" * 64,
        fit_dates=750,
        history_sessions=61,
        history_excludes_signal=True,
    )
    cache = _restart_cache(root)
    with cache.open() as samples:
        samples.add_final(
            TrainingSample(f"{i:06d}", day, "main", "bank", 1e8, tuple(float(j + i) for j in range(width)), (0.01,) * 6)
            for day in (split.model_fit_dates[0], split.early_stopping_dates[0])
            for i in range(5)
        )
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    sealed = cache.path.read_bytes(), cache.manifest.read_bytes()
    process = get_context("spawn").Process(target=_crash_tcn_cache, args=(root, point))
    process.start()
    try:
        process.join(30)
        assert process.exitcode == 86
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
    residue = tuple(root.glob("tcn-batches-*"))
    assert len(residue) == 1
    assert (residue[0] / "training").is_file()
    assert (residue[0] / "early_stopping").exists() == (point == "ready")
    with HistoryMaintenanceLock(tmp_path / "models/.v5.training.lock"):
        reopened = _restart_cache(root)
        with reopened.open() as samples:
            assert reopened.reused and samples.count() == 10
            assert not tuple(root.glob("tcn-batches-*"))
            counts = samples.industry_counts(contract)[0]
            with batch_cache.CacheTcnBatches(samples, counts, contract, 2, lambda: None) as batches:
                assert sum(len(batch.labels) for batch in batches.batches("training")) == 5
    assert (cache.path.read_bytes(), cache.manifest.read_bytes()) == sealed


@pytest.mark.parametrize("foreign", ("file", "directory", "symlink"))
def test_recovery_preserves_unrecognized_tcn_files_and_all_owned_workspaces(tmp_path, foreign):
    root = tmp_path / "samples/v5"
    first = root / "tcn-batches-owned"
    first.mkdir(parents=True)
    owned = first / "training"
    owned.write_bytes(b"owned scratch")
    second = root / "tcn-batches-unrecognized"
    second.mkdir()
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep")
    if foreign == "file":
        (second / "unknown").write_bytes(b"keep")
    elif foreign == "directory":
        (second / "training").mkdir()
        (second / "training" / "unknown").write_bytes(b"keep")
    else:
        (second / "training").symlink_to(protected)
    with HistoryMaintenanceLock(tmp_path / "models/.v5.training.lock"):
        with pytest.raises(ValueError, match="unknown_tcn_workspace"):
            _restart_cache(root).open()
    assert owned.read_bytes() == b"owned scratch"
    assert second.exists()
    assert protected.read_bytes() == b"keep"
