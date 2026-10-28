"""The offline columnar candidate cannot change publication visibility or input values."""

from dataclasses import replace

import pytest

from scripts import diagnose_runtime
from scripts.runtime_diagnostics.history_parquet import ParquetHistoryProjection
from scripts.runtime_diagnostics.training_read_comparison import PrepareTrainingHistory, TrainingRowsHash
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_inspection import LoadPublishedHistorySnapshot
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader


@pytest.fixture(scope="module")
def history_root(tmp_path_factory):
    return PrepareTrainingHistory(tmp_path_factory.mktemp("training-input"), size=4, sessions=64)


def _sqlite_rows(history):
    return HistorySnapshotReader(history.root).iter_training_rows(
        history.snapshot, history.calendar.open_dates, tuple(item.code for item in history.universe.securities)
    )


def test_month_reuse_atomic_failure_and_unpublished_revision_isolation(history_root, tmp_path, monkeypatch):
    first = LoadPublishedHistorySnapshot(history_root, sequence=1)
    second = LoadPublishedHistorySnapshot(history_root, sequence=2)
    projection = ParquetHistoryProjection(tmp_path)
    old = projection.publish(first)
    manifest = (tmp_path / "manifest.json").read_bytes()
    before = TrainingRowsHash(_sqlite_rows(first))
    assert TrainingRowsHash(projection.iter_rows(first, first.calendar.open_dates)) == before

    def fail(_publication):
        raise OSError("fixture publication failed")

    with monkeypatch.context() as patch:
        patch.setattr(projection, "_publish_manifest", fail)
        with pytest.raises(OSError, match="publication failed"):
            projection.publish(second)
    assert (tmp_path / "manifest.json").read_bytes() == manifest
    assert TrainingRowsHash(projection.iter_rows(first, first.calendar.open_dates)) == before
    current = projection.publish(second)
    assert sum(left == right for left, right in zip(old.months, current.months, strict=True)) == len(old.months) - 1
    assert TrainingRowsHash(projection.iter_rows(second, second.calendar.open_dates)) == TrainingRowsHash(_sqlite_rows(second))
    with pytest.raises(ValueError, match="snapshot_mismatch"):
        tuple(projection.iter_rows(first, first.calendar.open_dates))
    file = tmp_path / current.months[0].filename
    file.write_bytes(file.read_bytes() + b"bad")
    with pytest.raises(ValueError, match="hash_mismatch"):
        tuple(projection.iter_rows(second, second.calendar.open_dates))


def test_missing_price_and_zero_amount_keep_nulls_and_exact_float_values(history_root, tmp_path):
    history = LoadPublishedHistorySnapshot(history_root, sequence=2)
    reference = history.snapshot.partitions[-1]
    partition = HistoryPricePartition(history.root / reference.relative_path, reference.calendar_year, reference.calendar_month)
    day = history.calendar.open_dates[-1]
    original = partition.read_code("600000", day, day, snapshot_sequence=2)[0]
    missing = replace(original.cell, status="qfq_missing", qfq=None)
    other = partition.read_code("600001", day, day, snapshot_sequence=2)[0]
    assert other.cell.unadjusted is not None and other.cell.qfq is not None
    zero = replace(
        other.cell, unadjusted=replace(other.cell.unadjusted, volume=0.0),
        qfq=replace(other.cell.qfq, amount=0.0),
    )
    partition.save_revisions((
        HistoryRevision(4, "main", missing, False, "fixture", "fixture"),
        HistoryRevision(4, "main", zero, False, "fixture", "fixture"),
    ))
    snapshot = replace(history.snapshot, sequence=4, partitions=(*history.snapshot.partitions[:-1], partition.seal(4)))
    HistoryControlIndex(history.root / "catalog.sqlite3").publish_snapshot(snapshot)
    updated = LoadPublishedHistorySnapshot(history.root, sequence=4)
    projection = ParquetHistoryProjection(tmp_path)
    projection.publish(updated)
    rows = tuple(projection.iter_rows(updated, updated.calendar.open_dates))
    assert TrainingRowsHash(rows) == TrainingRowsHash(_sqlite_rows(updated))
    assert any(row.point is None and row.cell_status == "qfq_missing" for row in rows)
    assert any(row.point is not None and row.point.qfq_amount == 0.0
               and row.point.raw_volume == 0.0 for row in rows)


def test_public_training_sample_probe_is_explicit_and_runs_only_its_worker(tmp_path):
    parser = diagnose_runtime._parser()
    args = parser.parse_args([
        "--profile", "training-samples", "--training-probe-directory", str(tmp_path / "probe"),
        "--baseline-checkout", str(tmp_path / "baseline"),
    ])
    options, _ = diagnose_runtime._validate(args)
    commands = diagnose_runtime.build_commands(options)
    assert len(commands) == 1 and commands[0].name == "training_samples"
    assert commands[0].argv[1:3] == ("-m", "scripts.runtime_diagnostics.training_samples")
