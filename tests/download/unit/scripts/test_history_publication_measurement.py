from __future__ import annotations

from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.history.history_publication_measurement import HistoryPublicationMeasurement
from scripts.download.history.history_publication_measurement import ProfilePublishedHistory
from scripts.download.history.history_write_benchmark import HistoryFixtureSupplier
from scripts.download.history.history_write_benchmark import MeasureHistoryWrites
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.infra.settings.data_paths import DataPaths
from trader.infra.sqlite_publication import SqlitePublicationError


def _published_fixture(directory: Path) -> Path:
    days = tuple(date(2025, 12, 29) + timedelta(days=index) for index in range(8))
    policy = SqlitePolicyFixture()
    supplier = HistoryFixtureSupplier(days, 2, sqlite_policy=policy)
    config = replace(
        LoadHistorySyncConfiguration(PROJECT_ROOT, DataPaths(directory / "data")),
        history_root=directory / "history", sessions=len(days), minimum_free_bytes=0,
    )
    supplier.publish_reference(config.reference_root / "company_st.sqlite3", days[-1], len(days))
    observed = datetime.combine(days[-1], time(20, 30), ZoneInfo("Asia/Shanghai"))
    result = RunHistorySync(config, supplier, clock=lambda: observed, sqlite_policy=policy)
    assert result.state == "completed"
    return config.history_root


def test_real_republication_uses_copies_and_preserves_input_bytes(tmp_path: Path) -> None:
    root = _published_fixture(tmp_path)
    inputs = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
    report = ProfilePublishedHistory(root, sqlite_policy=SqlitePolicyFixture())
    assert report["status"] == "passed"
    summary = report["summary"]
    assert summary["fixture"] is False
    assert len(summary["files"]) == 3
    assert all(item["counts_and_bytes_equal"] for item in summary["files"])
    assert {path: path.read_bytes() for path in inputs} == inputs
    publication = summary["publication"]
    assert publication["performance_acceptance"] is False
    annual = [item for item in publication["phases"] if item["phase"] == "annual_seal"]
    assert {item["file"] for item in annual} == {"prices_2025.sqlite3", "prices_2026.sqlite3"}
    assert all(item["operation_calls"]["_digest"] == 1 for item in annual)
    assert all(item["operation_calls"]["PublishSqliteFile"] == 1 for item in annual)
    assert all(item["primitive_seconds"]["sha256_update"] > 0 for item in annual)
    assert all(item["primitive_seconds"]["sqlite_execute_fetch"] > 0 for item in annual)
    assert all(item["primitive_seconds"]["fsync"] > 0 for item in annual)


def test_missing_source_receipt_is_rejected_without_signing_it(tmp_path: Path) -> None:
    root = _published_fixture(tmp_path)
    receipt = root / "prices_2025.sqlite3.sha256"
    receipt.unlink()
    before = (root / "prices_2025.sqlite3").read_bytes()
    with pytest.raises(SqlitePublicationError, match="file_missing"):
        ProfilePublishedHistory(root, sqlite_policy=SqlitePolicyFixture())
    assert not receipt.exists()
    assert (root / "prices_2025.sqlite3").read_bytes() == before


def test_sync_profile_separates_duplicate_catalog_seals_and_keeps_original_results() -> None:
    baseline = MeasureHistoryWrites(2, 5, sqlite_policy=SqlitePolicyFixture())
    profiled = MeasureHistoryWrites(2, 5, sqlite_policy=SqlitePolicyFixture(), publication_profile=True)
    assert baseline["status"] == profiled["status"] == "passed"
    before = baseline["summary"]["cases"]
    after = profiled["summary"]["cases"]
    assert [(item["case"], item["rows"], item["publication_hash"], item["requests"]) for item in before] == [
        (item["case"], item["rows"], item["publication_hash"], item["requests"]) for item in after
    ]
    initial = after[0]
    assert initial["sync_seconds"] <= initial["seconds"] + 0.001
    phases = initial["publication"]["phases"]
    assert len([item for item in phases if item["phase"] == "catalog_seal"]) == 2
    catalog_files = [item for item in phases if item["phase"] == "file_seal" and item["file"] == "catalog.sqlite3"]
    assert len(catalog_files) == 2
    assert catalog_files[0]["database_sha256"] == catalog_files[1]["database_sha256"]
    assert any(item["phase"] == "annual_seal" and item["parent_id"] is not None for item in phases)


def test_cancellation_restores_hooks_and_profiler() -> None:
    seal = HistoryPricePartition.seal_months
    previous = sys.getprofile()
    measurement = HistoryPublicationMeasurement()
    with pytest.raises(KeyboardInterrupt), measurement, measurement.phase("cancel", "history"):
        raise KeyboardInterrupt
    assert HistoryPricePartition.seal_months is seal
    assert sys.getprofile() is previous
    assert measurement.report()["phases"][0]["completed"] is False


def test_parallel_phases_keep_distinct_parent_stacks() -> None:
    barrier = Barrier(2)
    measurement = HistoryPublicationMeasurement()

    def measure(label: str) -> None:
        with measurement.phase("outer", label):
            barrier.wait(timeout=5)
            with measurement.phase("inner", label):
                assert sys.getprofile() is not None
        assert sys.getprofile() is None

    with measurement, ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(measure, ("first", "second")))
    phases = measurement.report()["phases"]
    assert len({item["id"] for item in phases}) == 4
    for child in (item for item in phases if item["phase"] == "inner"):
        assert phases[child["parent_id"]]["file"] == child["file"]
        assert child["profile_owned_here"] is False


def test_unified_entrypoint_passes_only_explicit_publication_profile(tmp_path: Path) -> None:
    selection = ["--business", "download", "--module", "history", "--action", "write"]
    arguments = [*selection, "--history-publication-profile", "--history-root", str(tmp_path)]
    options = ParseBusinessOptions(ParseToolArguments(arguments))
    command = BuildToolCommands(options)[0]
    assert "--publication-profile" in command.argv
    assert command.argv[command.argv.index("--history-root") + 1] == str(tmp_path)
    assert command.argv[command.argv.index("--runtime-config") + 1] == str(options.runtime_config)
    plain = ParseBusinessOptions(ParseToolArguments(selection))
    assert "--publication-profile" not in BuildToolCommands(plain)[0].argv
    fixture = ParseBusinessOptions(ParseToolArguments([*selection, "--history-publication-profile"]))
    assert "--history-root" not in BuildToolCommands(fixture)[0].argv
    with pytest.raises(ValueError, match="layout comparison"):
        ParseBusinessOptions(ParseToolArguments([*arguments, "--baseline-checkout", str(tmp_path)]))
    with pytest.raises(SystemExit) as error:
        ParseToolArguments([
            "--business", "infra", "--module", "database", "--action", "baseline", "--history-publication-profile",
        ])
    assert error.value.code == 2
