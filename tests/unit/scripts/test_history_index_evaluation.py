from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics.history_index_evaluation import EvaluateHistoryIndexes
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_prices import HistoryPricePartition
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


def _source(root: Path) -> Path:
    root.mkdir()
    path = root / "prices_2026.sqlite3"
    partition = HistoryPricePartition(path, 2026, 9)
    partition.initialize()
    for sequence in (1, 2):
        day = date(2026, 9, 10)
        raw = BaoStockDailySide("600001", day, "unadjusted", 10, 12, 9, 10 + sequence, 100, 1000, 10, 1, 2, "trading")
        qfq = BaoStockDailySide("600001", day, "qfq", 10, 12, 9, 10 + sequence, 100, 1000, None, None, None, "trading")
        partition.save_revisions(
            (
                HistoryRevision(
                    sequence, "main", BaoStockDailyCell("600001", day, "complete", raw, qfq), None, None, None
                ),
            )
        )
    partition.save_revisions(
        (HistoryRevision(2, "star", BaoStockDailyCell("688001", day, "unknown_missing", None, None), None, None, None),)
    )
    return path


def test_probe_preserves_source_revisions_and_restores_schema_before_sealing(tmp_path: Path) -> None:
    root = tmp_path / "history"
    source = _source(root)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    report = EvaluateHistoryIndexes(root, tmp_path / "probe", rounds=2, sample_count=5)
    assert report["equal"] is True
    assert report["schema_rejects_missing_index"] is True
    assert report["production_change_applied"] is False
    assert report["source_publication_verified"] is False
    assert report["source_records"] == 3
    assert report["sample_rows"] == 2
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert len(report["indexes"]) == 5
    queries = report["queries"]
    assert all(item["equal"] for item in queries)
    assert any(item["workload"].startswith("resume_keys:") for item in queries)
    assert any(item["workload"].startswith("training_month:") for item in queries)
    assert any(
        item["workload"].startswith("missing_dates:") and item["runs"]["baseline"][0]["rows"] == 1 for item in queries
    )
    for label in ("eager", "deferred"):
        assert len(report["writes"][label]) == 2
        assert all(run["row_count"] == 2 for run in report["writes"][label])
    with SqliteSession(tmp_path / "probe/candidate.sqlite3", SqliteConnectionOptions(read_only=True)) as connection:
        assert (
            connection.run(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name='history_price_date_board_code_idx'"
            ).require_one()[0]
            == 1
        )


@pytest.mark.parametrize("location", ("inside_source", "parent_source", "existing", "symlink"))
def test_probe_rejects_unsafe_destination(tmp_path: Path, location: str) -> None:
    root = tmp_path / "history"
    source = _source(root)
    destination = tmp_path / "probe"
    if location == "inside_source":
        destination = root / "probe"
    elif location == "parent_source":
        destination = tmp_path
    elif location == "existing":
        destination.mkdir()
    else:
        destination.symlink_to(root, target_is_directory=True)
    before = source.read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        EvaluateHistoryIndexes(root, destination)
    assert source.read_bytes() == before


def test_index_profile_requires_external_workspace_and_passes_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires --database-probe-directory"):
        _validate(_parser().parse_args(["--profile", "history-indexes"]))
    options, _ = _validate(
        _parser().parse_args(
            [
                "--profile",
                "history-indexes",
                "--database-probe-directory",
                str(tmp_path / "probe"),
                "--sqlite-query-rounds",
                "5",
                "--sqlite-revision-write-sample-count",
                "5000",
            ]
        )
    )
    (command,) = build_commands(options, python_executable="/python3")
    assert command.name == "history_index_evaluation"
    assert command.database_probe_directory == tmp_path / "probe"
    assert command.argv[-4:] == ("--query-rounds", "5", "--write-sample-count", "5000")
