"""Hard exits recover owned V1 scratch files without deleting unknown objects."""

from tests.helpers.training.training_resources import TrainingSampleSettingsFixture

import os
from contextlib import ExitStack, closing
from datetime import date
from multiprocessing import get_context
from pathlib import Path

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import SqliteTransaction
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.d25.v1.samples import LearnedRankMatrixRequest, LearnedRankSampleIndex
from trader.training.infra.sample_cache import TemporaryTrainingSamples

_DAY = date(2026, 1, 5)
_NAMESPACE = ".temporary-training"


def _populate(index: LearnedRankSampleIndex) -> None:
    with SqliteTransaction(index._connection):
        index._connection.run(
            "INSERT INTO samples VALUES (" + ",".join("?" for _ in range(17)) + ")",
            (EncodeDate(_DAY), "600001", "bank", *(float(number) for number in range(10)), 0, 0.1, 0.2, 1),
        )


def _crash_v1_samples(samples_root: Path, point: str) -> None:
    with HistoryMaintenanceLock(samples_root.parent / "models/.v1.training.lock"):
        with TemporaryTrainingSamples(samples_root, "v1") as directory:
            if point == "empty":
                os._exit(86)
            with closing(LearnedRankSampleIndex(directory / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())) as index:
                _populate(index)
                if point == "samples":
                    os._exit(86)
                with ExitStack() as resources:
                    index.read_matrices(
                        tuple(
                            LearnedRankMatrixRequest((_DAY,), strategy)
                            for strategy in (Strategy.TOMORROW, Strategy.D25)
                            for _ in range(4)
                        ),
                        directory / "matrices",
                        resources,
                    )
                    if point == "d25_probability":
                        index.read_matrices(
                            tuple(LearnedRankMatrixRequest((_DAY,), Strategy.D25, True) for _ in range(3)),
                            directory / "d25-probability", resources,
                        )
                    os._exit(86)


@pytest.mark.parametrize("point", ("empty", "samples", "matrices", "d25_probability"))
def test_hard_exit_releases_lock_and_rebuilds_v1_samples(tmp_path, point):
    samples_root = tmp_path / "samples"
    process = get_context("spawn").Process(target=_crash_v1_samples, args=(samples_root, point))
    process.start()
    try:
        process.join(30)
        assert process.exitcode == 86
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
    residue = tuple((samples_root / "v1" / _NAMESPACE).iterdir())
    assert len(residue) == 1
    assert (residue[0] / "samples.sqlite3").exists() == (point != "empty")
    assert len(tuple((residue[0] / "matrices").glob("*/*.bin"))) == (16 if point in ("matrices", "d25_probability") else 0)
    with HistoryMaintenanceLock(tmp_path / "models/.v1.training.lock"):
        if point == "d25_probability":
            probability = residue[0] / "d25-probability"
            assert len(tuple(probability.glob("*/*.bin"))) == 6
            (probability / "2/targets.bin").unlink()
        with TemporaryTrainingSamples(samples_root, "v1") as directory:
            assert not residue[0].exists()
            assert not tuple(directory.iterdir())
            with closing(LearnedRankSampleIndex(directory / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())) as index:
                _populate(index)
                with ExitStack() as resources:
                    matrices = index.read_matrices(
                        (LearnedRankMatrixRequest((_DAY,), Strategy.TOMORROW),), directory / "matrices", resources
                    )
                    assert matrices[0].targets.tolist() == [0.1]
    assert not samples_root.exists()


@pytest.mark.parametrize("cancelled", (False, True))
def test_v1_success_and_cancellation_close_mappings_and_remove_temporary_samples(tmp_path, cancelled):
    samples_root = tmp_path / "samples"
    buffers = []

    def execute() -> None:
        with HistoryMaintenanceLock(tmp_path / "models/.v1.training.lock"):
            with TemporaryTrainingSamples(samples_root, "v1") as directory:
                with closing(LearnedRankSampleIndex(directory / "samples.sqlite3", sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())) as index:
                    _populate(index)
                    with ExitStack() as resources:
                        matrices = index.read_matrices(
                            (LearnedRankMatrixRequest((_DAY,), Strategy.TOMORROW),), directory / "matrices", resources
                        )
                        probability = index.read_matrices(
                            (LearnedRankMatrixRequest((_DAY,), Strategy.D25, True),),
                            directory / "d25-probability", resources,
                        )
                        for matrix in (*matrices, *probability):
                            buffers.extend((matrix.features.base, matrix.targets.base))
                        if cancelled:
                            raise InterruptedError("fixture cancelled")

    if cancelled:
        with pytest.raises(InterruptedError, match="fixture cancelled"):
            execute()
    else:
        execute()
    assert len(buffers) == 4 and all(buffer.closed for buffer in buffers)
    assert not samples_root.exists()


@pytest.mark.parametrize("point", (
    "namespace", "workspace", "matrix", "file_symlink", "workspace_symlink",
    "d25_probability_partition", "d25_probability_symlink",
))
def test_v1_recovery_validates_all_workspaces_before_deleting_any(tmp_path, point):
    samples_root = tmp_path / "samples"
    namespace = samples_root / "v1" / _NAMESPACE
    good = namespace / ("training-" + "a" * 32)
    good.mkdir(parents=True)
    retained = good / "samples.sqlite3"
    retained.write_bytes(b"owned scratch")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "keep").write_bytes(b"keep")
    bad = namespace / ("training-" + "b" * 32)
    if point == "namespace":
        (namespace / "unknown").write_bytes(b"keep")
    elif point == "workspace_symlink":
        bad.symlink_to(protected, target_is_directory=True)
    else:
        bad.mkdir()
        if point == "workspace":
            (bad / "unknown").write_bytes(b"keep")
        elif point == "file_symlink":
            (bad / "samples.sqlite3").symlink_to(protected / "keep")
        elif point == "d25_probability_partition":
            (bad / "d25-probability/3").mkdir(parents=True)
        elif point == "d25_probability_symlink":
            partition = bad / "d25-probability/0"
            partition.mkdir(parents=True)
            (partition / "targets.bin").symlink_to(protected / "keep")
        else:
            matrix = bad / "matrices/0"
            matrix.mkdir(parents=True)
            (matrix / "unknown").write_bytes(b"keep")
    with HistoryMaintenanceLock(tmp_path / "models/.v1.training.lock"):
        with pytest.raises(ValueError, match="contains existing data"):
            with TemporaryTrainingSamples(samples_root, "v1"):
                pytest.fail("unknown scratch data was accepted")
    assert retained.read_bytes() == b"owned scratch"
    assert (protected / "keep").read_bytes() == b"keep"


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_only_v1_owns_d25_probability_workspace(tmp_path, profile):
    samples_root = tmp_path / "samples"
    workspace = samples_root / profile / _NAMESPACE / ("training-" + "a" * 32)
    partition = workspace / "d25-probability/0"
    partition.mkdir(parents=True)
    retained = partition / "targets.bin"
    retained.write_bytes(b"unknown data")
    with HistoryMaintenanceLock(tmp_path / f"models/.{profile}.training.lock"):
        with pytest.raises(ValueError, match="contains existing data"):
            with TemporaryTrainingSamples(samples_root, profile):
                pytest.fail("another profile accepted V1's probability workspace")
    assert retained.read_bytes() == b"unknown data"


def test_v1_recovery_rejects_unowned_profile_data_and_namespace_symlinks(tmp_path):
    samples_root = tmp_path / "samples"
    profile_root = samples_root / "v1"
    profile_root.mkdir(parents=True)
    unknown = profile_root / "existing.sqlite3"
    unknown.write_bytes(b"keep")
    with pytest.raises(ValueError, match="contains existing data"):
        with TemporaryTrainingSamples(samples_root, "v1"):
            pytest.fail("unowned profile data was accepted")
    assert unknown.read_bytes() == b"keep"
    unknown.unlink()
    protected = tmp_path / "protected"
    protected.mkdir()
    (profile_root / _NAMESPACE).symlink_to(protected, target_is_directory=True)
    with pytest.raises(ValueError, match="contains existing data"):
        with TemporaryTrainingSamples(samples_root, "v1"):
            pytest.fail("namespace symlink was accepted")
    assert protected.is_dir()
