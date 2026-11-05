"""Isolation and explicit invocation of the all-profile alignment probe."""

from scripts.checks.catalog import SelectionArguments

from tests.helpers.configuration import SqlitePolicyFixture

from pathlib import Path
from datetime import date
from contextlib import nullcontext
from threading import enumerate as enumerate_threads
import sys
import subprocess
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.checks.arguments import BuildParser
from scripts.checks.arguments import ValidateOptions
from scripts.checks.commands import BuildCommands
from scripts.checks.training.training_alignment import _probe


@pytest.mark.parametrize("reported_profile", ("v1", "v2"))
def test_training_probe_decodes_pretty_profile_and_execution_reports(tmp_path, monkeypatch, reported_profile):
    from scripts.checks.training import training_alignment
    from trader import bootstrap
    from trader.recommendation.infra.scoring import profile_factory

    profile = {"schema_version": "profile_training_result", "profile": reported_profile, "status": "engineering_ready", "message": "训练完成"}
    execution = {
        "schema_version": "selected_training_result",
        "status": "completed",
        "history_rows_read": 0,
        "timing": {"source_read_seconds": 0.0, "profiles": [{"profile": reported_profile, "queue_send_seconds": 1.25}]},
    }

    def train(*args, **kwargs):
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        print(json.dumps(execution, ensure_ascii=False, indent=2))
        return 0

    monkeypatch.setattr(bootstrap, "ExecuteSelectedTraining", train)
    monkeypatch.setattr(profile_factory, "LoadScoringProfile", lambda *args, **kwargs: SimpleNamespace(heads=(1, 2)))
    monkeypatch.setattr(training_alignment, "_numeric_fingerprints", lambda *args: ())
    result = training_alignment._train_once(None, tmp_path / "history", tmp_path / "work", ("v1",))
    assert result["complete"] is (reported_profile == "v1")
    assert result["profiles"] == [profile]
    assert result["execution"] == execution


def test_alignment_import_does_not_preload_the_product_before_checkout_selection():
    completed = subprocess.run(
        [sys.executable, "-c", "import sys; import scripts.checks.training.training_alignment; assert 'trader' not in sys.modules"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_all_profile_probe_requires_external_isolated_history_and_new_output(tmp_path: Path):
    with pytest.raises(ValueError, match="requires --history-root"):
        ValidateOptions(BuildParser().parse_args([*SelectionArguments('training-alignment')]))
    args = [
        *SelectionArguments('training-alignment'),
        "--history-root",
        str(tmp_path / "history"),
        "--training-probe-directory",
        str(tmp_path / "probe"),
    ]
    options, _ = ValidateOptions(BuildParser().parse_args(args))
    commands = BuildCommands(options)
    assert commands[0].name == "training_alignment"
    assert "scripts.checks.training.training_alignment" in commands[0].argv
    with pytest.raises(ValueError, match="external_history_copy"):
        _probe(Path(__file__).resolve().parents[3] / "data/market/history", tmp_path / "probe", sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(ValueError, match="new_separate_output"):
        _probe(tmp_path / "history", tmp_path, sqlite_policy=SqlitePolicyFixture())


@pytest.mark.parametrize("inputs_only", (True, False))
@pytest.mark.parametrize("explicit_config", (True, False))
@pytest.mark.parametrize("benchmark_mode", (None, "baseline_before", "baseline_after", "candidate"))
def test_entry_selects_checkout_before_loading_its_sqlite_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inputs_only: bool, explicit_config: bool, benchmark_mode: str | None,
):
    from scripts.checks.training import training_alignment as module
    import trader.bootstrap as bootstrap
    import trader.infra.settings as settings

    product = tmp_path / "product"
    policy_module = product / "src/trader/infra/sqlite_policy.py"
    policy_module.parent.mkdir(parents=True)
    policy_module.touch()
    config = tmp_path / "custom.json" if explicit_config else product / "config/runtime.json"
    policy = SqlitePolicyFixture()
    calls = []

    def select(checkout):
        assert checkout == product
        calls.append("selected")
        return product

    def load(path):
        assert calls == ["selected"]
        assert path == config
        calls.append("loaded")
        return policy

    def inputs(history, runtime_config, *, sqlite_policy):
        assert calls == ["selected", "loaded"]
        assert runtime_config == config
        assert sqlite_policy is policy
        return {"status": "passed"}

    def probe(history, work, profiles, *, runtime_config, sqlite_policy, require_reuse):
        assert require_reuse is (benchmark_mode not in {"baseline_before", "baseline_after"})
        return inputs(history, runtime_config, sqlite_policy=sqlite_policy)

    argv = ["probe", "--history-root", str(tmp_path / "history"), "--product-checkout", str(product)]
    argv += ["--inputs-only"] if inputs_only else ["--work-root", str(tmp_path / "output")]
    if explicit_config:
        argv += ["--runtime-config", str(config)]
    if benchmark_mode is not None:
        argv += ["--benchmark-mode", benchmark_mode]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "_select_product", select)
    monkeypatch.setattr(settings, "LoadRuntimeSettings", load)
    monkeypatch.setattr(bootstrap, "BuildSqlitePolicy", lambda loaded: loaded)
    monkeypatch.setattr(module, "_inputs", inputs)
    monkeypatch.setattr(module, "_probe", probe)
    monkeypatch.setattr(module, "emit_report", lambda report: None)
    assert module.Main() == 0


@pytest.mark.parametrize("source_changed", (False, True))
def test_history_copy_includes_committed_wal_and_references_without_source_writes(
    tmp_path, monkeypatch, source_changed
):
    from scripts.checks.training.training_alignment import PrepareTrainingHistoryCopy
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    market = tmp_path / "source/market"
    history = market / "history"
    reference = market / "reference"
    history.mkdir(parents=True)
    reference.mkdir()
    paths = (history / "catalog.sqlite3", history / "prices_2026.sqlite3",
             reference / "company_st.sqlite3", reference / "industry.sqlite3")
    connections = []
    for path in paths:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fixture(value INTEGER)")
        connection.execute("INSERT INTO fixture VALUES(17)")
        connection.commit()
        connections.append(connection)
    connections[0].execute("CREATE TABLE schema_contract(singleton INTEGER PRIMARY KEY,reference_file TEXT)")
    connections[0].execute("INSERT INTO schema_contract VALUES(1,'../reference/company_st.sqlite3')")
    connections[0].commit()
    before = tuple(path.read_bytes() for path in paths)
    snapshot = SimpleNamespace(
        snapshot_hash="a" * 64, source_identity_hash="b" * 64,
        source_cutoff=date(2026, 11, 4), training_codes=("600001",),
    )
    calls = []

    def opened(path, *, sqlite_policy):
        calls.append(path)
        value = SimpleNamespace(snapshot_hash="changed") if source_changed and len(calls) == 3 else snapshot
        return SimpleNamespace(snapshot=value)

    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", opened)
    target = tmp_path / "copy"
    try:
        if source_changed:
            with pytest.raises(ValueError, match="source_changed"):
                PrepareTrainingHistoryCopy(history, target, timeout_seconds=30, sqlite_policy=SqlitePolicyFixture())
            assert not (target / "copy-summary.json").exists()
        else:
            report = PrepareTrainingHistoryCopy(history, target, timeout_seconds=30, sqlite_policy=SqlitePolicyFixture())
            assert report["files"] == 4 and report["source_data_writes"] == 0
            assert report["source_identity_unchanged"] is True
            for path in paths:
                with sqlite3.connect((target / path.relative_to(market)).as_uri() + "?mode=ro", uri=True) as copied:
                    assert copied.execute("SELECT value FROM fixture").fetchone() == (17,)
        assert tuple(path.read_bytes() for path in paths) == before
        assert all(connection.execute("SELECT value FROM fixture").fetchone() == (17,) for connection in connections)
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("invalid", ("absolute", "escape", "copy_escape"))
def test_history_copy_rejects_reference_binding_outside_the_copied_layout(tmp_path, monkeypatch, invalid):
    from scripts.checks.training.training_alignment import PrepareTrainingHistoryCopy
    from trader.infra.sqlite_connection import SqliteConnection
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    market = tmp_path / "source"
    history = market / "history"
    reference = market / "reference"
    history.mkdir(parents=True)
    reference.mkdir()
    paths = (history / "catalog.sqlite3", history / "prices_2026.sqlite3",
             reference / "company_st.sqlite3", reference / "industry.sqlite3")
    for path in paths:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE fixture(value INTEGER)")
    binding = str(paths[2]) if invalid == "absolute" else "../../shared/company_st.sqlite3"
    with sqlite3.connect(paths[0]) as connection:
        connection.execute("CREATE TABLE schema_contract(singleton INTEGER PRIMARY KEY,reference_file TEXT)")
        connection.execute("INSERT INTO schema_contract VALUES(1,?)", (
            "../reference/company_st.sqlite3" if invalid == "copy_escape" else binding,
        ))
    original_backup = SqliteConnection.backup_to
    backups = []

    def backup(original, target, *, deadline, monotonic):
        original_backup(original, target, deadline=deadline, monotonic=monotonic)
        backups.append(target)
        if len(backups) == 1:
            target.run("UPDATE schema_contract SET reference_file=?", (binding,))

    monkeypatch.setattr(SqliteConnection, "backup_to", backup)
    opened = []

    def snapshot(path, *, sqlite_policy):
        opened.append(path)
        assert invalid == "copy_escape" and path == history and len(opened) == 1
        return SimpleNamespace(snapshot=object())

    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", snapshot)
    destination = tmp_path / "copy"
    before = paths[0].read_bytes()
    with pytest.raises(ValueError, match="training_history_copy_reference_invalid"):
        PrepareTrainingHistoryCopy(history, destination, timeout_seconds=30, sqlite_policy=SqlitePolicyFixture())
    assert not (destination / "copy-summary.json").exists()
    assert paths[0].read_bytes() == before
    assert opened == ([history] if invalid == "copy_escape" else [])
    assert bool(backups) == (invalid == "copy_escape")


@pytest.mark.parametrize("failure", (False, True))
def test_training_resource_observer_stops_on_success_and_exception(tmp_path, failure):
    from scripts.checks.training.training_process_measurement import TrainingProcessMeasurement

    (tmp_path / "status").write_text("VmRSS: 1024 kB\nThreads: 4\n")
    (tmp_path / "io").write_text("read_bytes: 10\nwrite_bytes: 20\n")
    measurement = TrainingProcessMeasurement(proc_root=tmp_path)
    with pytest.raises(ValueError, match="fixture") if failure else nullcontext():
        with measurement:
            (tmp_path / "status").write_text("VmRSS: 2048 kB\nThreads: 6\n")
            (tmp_path / "io").write_text("read_bytes: 40\nwrite_bytes: 90\n")
            if failure:
                raise ValueError("fixture")
    assert measurement.result["observed_peak_rss_bytes"] == 2048 * 1024
    assert measurement.result["observed_peak_os_threads"] == 6
    assert measurement.result["physical_read_bytes"] == 30
    assert measurement.result["physical_write_bytes"] == 70
    assert measurement.result["native_thread_utilization_verified"] is False
    assert not any(thread.name == "training-diagnostic-resources" for thread in enumerate_threads())


def test_training_resource_observer_keeps_unavailable_measurements_unknown(tmp_path):
    from scripts.checks.training.training_process_measurement import TrainingProcessMeasurement

    with TrainingProcessMeasurement(proc_root=tmp_path) as measurement:
        pass
    assert measurement.result["observed_peak_rss_bytes"] is None
    assert measurement.result["physical_read_bytes"] is None
    assert measurement.result["samples"] == 0
    assert measurement.result["sampling_failures"] == 2
