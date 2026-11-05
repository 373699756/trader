"""Private worker candidates use identical isolated inputs with optional cProfile."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.download.history import history_write_benchmark as worker
from scripts.download.history.history_publication_measurement import HistoryPublicationMeasurement, ProfilePublishedHistory
from tests.helpers.configuration import SqlitePolicyFixture
from trader.download.infra.download_settings import LoadHistorySyncConfiguration
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.infra.settings.data_paths import DataPaths


@pytest.fixture(scope="module")
def published_history(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("history-publication-candidates")
    days = tuple(date(2025, 12, 29) + timedelta(days=index) for index in range(8))
    policy = SqlitePolicyFixture()
    supplier = worker.HistoryFixtureSupplier(days, 2, sqlite_policy=policy)
    config = replace(
        LoadHistorySyncConfiguration(Path(__file__).resolve().parents[3], DataPaths(directory / "data")),
        history_root=directory / "history", sessions=len(days), minimum_free_bytes=0,
    )
    supplier.publish_reference(config.reference_root / "company_st.sqlite3", days[-1], len(days))
    observed = datetime.combine(days[-1], time(20, 30), ZoneInfo("Asia/Shanghai"))
    result = RunHistorySync(config, supplier, clock=lambda: observed, sqlite_policy=policy)
    assert result.state == "completed"
    return config.history_root


@pytest.mark.parametrize("candidate", ["baseline", "exact-month-ranges", "fingerprint-first", "combined"])
@pytest.mark.parametrize("profiler", [False, True])
def test_candidates_preserve_inputs_and_publish_equivalent_copies(
    published_history: Path, candidate: str, profiler: bool
) -> None:
    before = {path: path.read_bytes() for path in published_history.iterdir() if path.is_file()}
    report = ProfilePublishedHistory(
        published_history, sqlite_policy=SqlitePolicyFixture(), candidate=candidate, profiler=profiler
    )
    assert report["status"] == "passed"
    summary = report["summary"]
    assert summary["candidate"] == candidate
    assert summary["source_and_preparation_included"] is True
    assert len(summary["files"]) == 3
    assert all(item["counts_and_bytes_equal"] for item in summary["files"])
    measured = summary["publication"]
    assert measured["performance_acceptance"] is False
    assert measured["profiling_enabled"] is profiler
    annual = tuple(item for item in measured["phases"] if item["phase"] == "annual_seal")
    assert {item["file"] for item in annual} == {"prices_2025.sqlite3", "prices_2026.sqlite3"}
    for item in annual:
        assert item["exact_month_ranges"] is (candidate in {"exact-month-ranges", "combined"})
        assert item["fingerprint_before_statistics"] is (candidate in {"fingerprint-first", "combined"})
        if profiler:
            assert item["operation_calls"]["_digest"] == 1
            assert item["operation_calls"]["PrepareSqliteFilePublication"] == 1
            assert item["operation_calls"]["PublishPreparedSqliteFile"] == 1
        else:
            assert "operation_calls" not in item
            assert "primitive_seconds" not in item
    if candidate in {"fingerprint-first", "combined"}:
        for phase in ("file_fingerprint", "file_receipt"):
            parts = tuple(item for item in measured["phases"] if item["phase"] == phase)
            assert len(parts) == 2
            assert all(measured["phases"][item["parent_id"]]["phase"] == "annual_seal" for item in parts)
    assert {path: path.read_bytes() for path in before} == before


def test_profiler_off_restores_hooks_and_does_not_enable_python_profiling() -> None:
    before = sys.getprofile()
    measurement = HistoryPublicationMeasurement(profiler=False)
    with pytest.raises(KeyboardInterrupt), measurement, measurement.phase("cancel", "history"):
        assert sys.getprofile() is before
        raise KeyboardInterrupt
    assert sys.getprofile() is before
    phase = measurement.report()["phases"][0]
    assert phase["completed"] is False
    assert phase["profile_owned_here"] is False


def test_private_worker_passes_candidate_and_profiler_without_global_tool_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trader import bootstrap
    from trader.infra import settings

    captured: list[tuple[Path, str, bool]] = []

    def profile(root: Path, *, sqlite_policy: object, candidate: str, profiler: bool) -> dict[str, object]:
        captured.append((root, candidate, profiler))
        return {"status": "passed"}

    monkeypatch.setattr(settings, "LoadRuntimeSettings", lambda _path: None)
    monkeypatch.setattr(bootstrap, "BuildSqlitePolicy", lambda _settings: SqlitePolicyFixture())
    monkeypatch.setattr(worker, "ProfilePublishedHistory", profile)
    monkeypatch.setattr(worker, "emit_report", lambda _report: None)
    monkeypatch.setattr(sys, "argv", [
        "history-write", "--publication-profile", "--history-root", str(tmp_path),
        "--publication-candidate", "combined", "--publication-profiler", "off",
    ])
    assert worker.Main() == 0
    assert captured == [(tmp_path, "combined", False)]


@pytest.mark.parametrize("extra", [["--publication-candidate", "combined"], ["--publication-profiler", "off"]])
def test_candidate_options_require_an_explicit_isolated_source(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["history-write", "--publication-profile", *extra])
    with pytest.raises(SystemExit) as invalid:
        worker.Main()
    assert invalid.value.code == 2
