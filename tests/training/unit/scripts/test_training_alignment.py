"""Isolation and explicit invocation of the all-profile alignment probe."""

from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments

from scripts.core.catalog import SelectionArguments

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture

from pathlib import Path
from datetime import date, datetime, timezone
from contextlib import nullcontext
from threading import enumerate as enumerate_threads
import sys
import subprocess
import json
import os
import sqlite3
import time
from types import SimpleNamespace
from dataclasses import replace

import pytest

from scripts.training.jobs.training_alignment import _probe
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions, SqliteSession, SqliteTransaction


def _publish_industry(history, sqlite_policy):
    from urllib.parse import parse_qs, urlsplit
    from zoneinfo import ZoneInfo
    from trader.download.domain.baostock_daily import BaoStockSecurity
    from trader.download.infra.cninfo_industry import CninfoIndustryClient
    from trader.download.infra.current_industry_publication import CurrentIndustryPublication
    from trader.download.infra.history_snapshot_inspection import LoadPublishedHistorySnapshot
    from trader.infra.http_contracts import HttpResponse
    from trader.infra.http_transport import RequestsHttpTransport

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    standard = "中国上市公司协会上市公司行业分类标准"
    published = LoadPublishedHistorySnapshot(history, sqlite_policy=sqlite_policy)
    universe = tuple(BaoStockSecurity(item.code, item.code, "main", date(2000, 1, 1), None, "fixture")
                     for item in published.universe.securities)

    def execute(request):
        parsed = urlsplit(request.url)
        if parsed.path.endswith("p_public0002"):
            rows = [{"SORTCODE": code, "PARENTCODE": parent, "SORTNAME": name, "F001V": None, "F002D": None,
                     "F003V": "008001", "F004V": standard}
                    for code, parent, name in (("0", "008", "中上协行业分类"), ("J", "0", "金融业"),
                                               ("J66", "J", "货币金融服务"), ("J661", "J66", "中央银行服务"))]
        else:
            code = parse_qs(parsed.query)["scode"][0]
            rows = [{"SECCODE": code, "SECNAME": "测试股份", "ORGNAME": "测试股份有限公司", "VARYDATE": "2024-09-30",
                     "F001V": "008001", "F002V": standard, "F003V": "J661", "F004V": "金融业", "F005V": None,
                     "F006V": "货币金融服务", "F007V": "中央银行服务", "F008C": "1"}]
        return HttpResponse(200, json.dumps({"resultcode": 200, "count": len(rows), "records": rows}, ensure_ascii=False).encode())

    with RequestsHttpTransport(execute_request=execute) as transport:
        CurrentIndustryPublication(
            history.parent / "reference/industry.sqlite3", CninfoIndustryClient(
                transport, request_headers=lambda: (("Accept-Enckey", "fixture"),),
            ), sqlite_policy=sqlite_policy, clock=lambda: now,
        ).fetch(now, universe, (date(2024, 1, 1), now.date()))


@pytest.mark.parametrize("reported_profile", ("v1", "v2"))
def test_training_probe_decodes_pretty_profile_and_execution_reports(tmp_path, monkeypatch, reported_profile):
    from scripts.training.jobs import training_alignment
    from trader import bootstrap
    from trader.recommendation.infra.scoring import profile_factory

    profile = {"schema_version": "profile_training_result", "profile": reported_profile, "status": "engineering_ready", "message": "训练完成"}
    execution = {
        "schema_version": "selected_training_result",
        "status": "completed",
        "history_rows_read": 0,
        "timing": {"source_read_seconds": 0.0, "profiles": [{"profile": reported_profile, "queue_send_seconds": 1.25}]},
    }
    industry = CurrentIndustrySnapshotFixture(("600001",))

    def train(*args, **kwargs):
        assert kwargs["industry_snapshot"] is industry
        print(json.dumps(profile, ensure_ascii=False, indent=2))
        print(json.dumps(execution, ensure_ascii=False, indent=2))
        return 0

    monkeypatch.setattr(bootstrap, "ExecuteSelectedTraining", train)
    monkeypatch.setattr(profile_factory, "LoadScoringProfile", lambda *args, **kwargs: SimpleNamespace(heads=(1, 2)))
    monkeypatch.setattr(training_alignment, "_numeric_fingerprints", lambda *args: ())
    result = training_alignment._train_once(None, tmp_path / "history", tmp_path / "work", ("v1",), industry_snapshot=industry)
    assert result["complete"] is (reported_profile == "v1")
    assert result["profiles"] == [profile]
    assert result["execution"] == execution


def test_alignment_import_does_not_preload_the_product_before_checkout_selection():
    completed = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import scripts.training.jobs.training_alignment; "
            "assert {name for name in sys.modules if name == 'trader' or name.startswith('trader.')} "
            "<= {'trader', 'trader.infra', 'trader.infra.linux_process'}"
        )],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_all_profile_probe_requires_external_isolated_history_and_new_output(tmp_path: Path):
    with pytest.raises(ValueError, match="requires --history-root"):
        ParseBusinessOptions(ParseToolArguments([*SelectionArguments('training-alignment')]))
    args = [
        *SelectionArguments('training-alignment'),
        "--history-root",
        str(tmp_path / "history"),
        "--training-probe-directory",
        str(tmp_path / "probe"),
    ]
    options = ParseBusinessOptions(ParseToolArguments(args))
    commands = BuildToolCommands(options)
    assert commands[0].name == "training_alignment"
    assert "scripts.training.jobs.training_alignment" in commands[0].argv
    with pytest.raises(ValueError, match="external_history_copy"):
        _probe(Path(__file__).resolve().parents[4] / "data/market/history", tmp_path / "probe", sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(ValueError, match="new_separate_output"):
        _probe(tmp_path / "history", tmp_path, sqlite_policy=SqlitePolicyFixture())


@pytest.mark.parametrize("failure", ("current_industry_not_published", "current_industry_publication_expired"))
def test_probe_rejects_unavailable_current_industry_before_creating_output(tmp_path, monkeypatch, failure):
    from trader import bootstrap
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

    def unavailable(*_args):
        raise ValueError(failure)

    def forbidden(*_args, **_kwargs):
        pytest.fail("unavailable current industry must not open a training input")

    monkeypatch.setattr(bootstrap, "ReadIndustrySnapshot", unavailable)
    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", forbidden)
    work = tmp_path / "output"
    with pytest.raises(ValueError, match=failure):
        _probe(tmp_path / "history", work, ("v1",), sqlite_policy=SqlitePolicyFixture())
    assert not work.exists()


@pytest.mark.parametrize("inputs_only", (True, False))
@pytest.mark.parametrize("explicit_config", (True, False))
@pytest.mark.parametrize("benchmark_mode", (None, "baseline_before", "baseline_after", "candidate"))
def test_entry_selects_checkout_before_loading_its_sqlite_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inputs_only: bool, explicit_config: bool, benchmark_mode: str | None,
):
    from scripts.training.jobs import training_alignment as module
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


@pytest.mark.parametrize("inputs_only", (False, True))
def test_computation_candidate_is_explicit_and_requires_training_mode(tmp_path, monkeypatch, inputs_only):
    from scripts.training.jobs import training_alignment as module

    project = Path(__file__).resolve().parents[4]
    calls = []
    argv = ["probe", "--history-root", str(tmp_path / "history"), "--computation-candidate", "combined"]
    argv += ["--inputs-only"] if inputs_only else ["--work-root", str(tmp_path / "output")]

    def select(_checkout):
        calls.append("selected")
        return project

    def probe(*_args, computation_candidate, **_kwargs):
        assert calls == ["selected"] and computation_candidate == "combined"
        calls.append("probed")
        return {"status": "passed"}

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "_select_product", select)
    monkeypatch.setattr(module, "_probe", probe)
    monkeypatch.setattr(module, "emit_report", lambda _report: None)
    assert module.Main() == (1 if inputs_only else 0)
    assert calls == ([] if inputs_only else ["selected", "probed"])


@pytest.mark.parametrize("inputs_only", (False, True))
def test_selected_execution_and_fanout_are_forwarded_only_in_training_mode(tmp_path, monkeypatch, inputs_only):
    from scripts.training.jobs import training_alignment as module

    project = Path(__file__).resolve().parents[4]
    calls = []
    argv = [
        "probe", "--history-root", str(tmp_path / "history"), "--execution-candidate", "selected",
        "--execution-components", "normalized_tcn,resources", "--fanout-candidate", "batch_double",
    ]
    argv += ["--inputs-only"] if inputs_only else ["--work-root", str(tmp_path / "output")]

    def select(_checkout):
        calls.append("selected")
        return project

    def probe(*_args, execution_candidate, execution_components, fanout_candidate, **_kwargs):
        assert calls == ["selected"]
        assert execution_candidate == "selected" and execution_components == ("resources", "normalized_tcn")
        assert fanout_candidate == "batch_double"
        calls.append("probed")
        return {"status": "passed"}

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(module, "_select_product", select)
    monkeypatch.setattr(module, "_probe", probe)
    monkeypatch.setattr(module, "emit_report", lambda _report: None)
    assert module.Main() == (1 if inputs_only else 0)
    assert calls == ([] if inputs_only else ["selected", "probed"])


def test_baseline_resource_projection_uses_completed_runs_and_rejects_missing_old_evidence():
    from scripts.training.jobs.training_alignment import _baseline_execution_runs

    execution = {
        "profiles": ["v1", "v2", "v3", "v5"], "fitting_cpu_capacity": 3, "native_threads": 2,
        "profile_threads": {"v1": 2, "v2": 2, "v3": 2}, "memory_capacity_bytes": 1024,
        "fanout": {"batch_rows": 256, "queue_batches": 2},
    }
    run = {"execution": execution}
    scenario = {"preparation": run, "run": run}
    projected = _baseline_execution_runs(run, run, scenario)
    assert projected is not None and len(projected) == 4
    assert all(item["cpu_capacity"] == 3 and item["batch_rows"] == 256 for item in projected)
    del execution["memory_capacity_bytes"]
    assert _baseline_execution_runs(run, run, scenario) is None


@pytest.mark.parametrize("source_changed", (False, True))
def test_history_copy_includes_committed_wal_and_references_without_source_writes(
    tmp_path, monkeypatch, source_changed
):
    from scripts.training.jobs.training_alignment import PrepareTrainingHistoryCopy
    from trader.infra.sqlite_publication import PublishSqliteFile
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader import bootstrap

    market = tmp_path / "source/market"
    history = market / "history"
    reference = market / "reference"
    history.mkdir(parents=True)
    reference.mkdir()
    paths = (history / "catalog.sqlite3", history / "prices_2026.sqlite3",
             reference / "company_st.sqlite3", reference / "industry.sqlite3")
    connections = []
    for path in paths:
        connection = OpenSqliteConnection(path, SqliteConnectionOptions(journal_mode="WAL"), sqlite_policy=SqlitePolicyFixture())
        with SqliteTransaction(connection):
            connection.run("PRAGMA user_version=2")
            connection.run("CREATE TABLE fixture(value INTEGER)")
            connection.run("INSERT INTO fixture VALUES(17)")
        connections.append(connection)
    with SqliteTransaction(connections[0]):
        connections[0].run("CREATE TABLE schema_contract(singleton INTEGER PRIMARY KEY,reference_file TEXT)")
        connections[0].run("INSERT INTO schema_contract VALUES(1,'../reference/company_st.sqlite3')")
    for path in paths[:2]:
        PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    before = tuple(path.read_bytes() for path in paths)
    industry = CurrentIndustrySnapshotFixture(("600001",))
    snapshot = SimpleNamespace(
        snapshot_hash="a" * 64, source_identity_hash="b" * 64,
        source_cutoff=date(2026, 11, 4), training_codes=("600001",),
        training_input_hash="c" * 64, industry_snapshot=industry,
    )
    calls = []

    def opened(path, *, industry_snapshot, sqlite_policy):
        assert industry_snapshot is industry
        calls.append(path)
        value = SimpleNamespace(snapshot_hash="changed") if source_changed and len(calls) == 3 else snapshot
        return SimpleNamespace(snapshot=value)

    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", opened)
    monkeypatch.setattr(bootstrap, "ReadIndustrySnapshot", lambda *_args: industry)
    monkeypatch.setattr("scripts.training.jobs.training_alignment.shutil.disk_usage", lambda _path: SimpleNamespace(free=2**40))
    target = tmp_path / "copy"
    try:
        if source_changed:
            with pytest.raises(ValueError, match="source_changed"):
                PrepareTrainingHistoryCopy(history, target, timeout_seconds=30, sqlite_policy=SqlitePolicyFixture())
            assert not (target / "copy-summary.json").exists()
        else:
            report = PrepareTrainingHistoryCopy(history, target, timeout_seconds=30, sqlite_policy=SqlitePolicyFixture())
            assert report["files"] == 4 and report["source_data_writes"] == 0
            assert report["publication_receipts"] == 2
            assert report["source_identity_unchanged"] is True
            assert report["training_input_hash"] == snapshot.training_input_hash
            assert report["industry_snapshot_hash"] == industry.content_hash
            for path in paths:
                with SqliteSession(target / path.relative_to(market), SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture()) as copied:
                    assert tuple(copied.run("SELECT value FROM fixture").require_one()) == (17,)
        assert tuple(path.read_bytes() for path in paths) == before
        assert all(tuple(connection.run("SELECT value FROM fixture").require_one()) == (17,) for connection in connections)
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("invalid", ("absolute", "escape", "copy_escape"))
def test_history_copy_rejects_reference_binding_outside_the_copied_layout(tmp_path, monkeypatch, invalid):
    from scripts.training.jobs.training_alignment import PrepareTrainingHistoryCopy
    from trader.infra.sqlite_connection import SqliteConnection
    from trader.infra.sqlite_publication import PublishSqliteFile
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader import bootstrap

    market = tmp_path / "source"
    history = market / "history"
    reference = market / "reference"
    history.mkdir(parents=True)
    reference.mkdir()
    paths = (history / "catalog.sqlite3", history / "prices_2026.sqlite3",
             reference / "company_st.sqlite3", reference / "industry.sqlite3")
    for path in paths:
        with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
            connection.run("CREATE TABLE fixture(value INTEGER)")
            connection.run("PRAGMA user_version=2")
    binding = str(paths[2]) if invalid == "absolute" else "../../shared/company_st.sqlite3"
    with SqliteSession(paths[0], sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE schema_contract(singleton INTEGER PRIMARY KEY,reference_file TEXT)")
        connection.run("INSERT INTO schema_contract VALUES(1,?)", (
            "../reference/company_st.sqlite3" if invalid == "copy_escape" else binding,
        ))
    for path in paths[:2]:
        PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    original_backup = SqliteConnection.backup_to
    backups = []

    def backup(original, target, *, deadline, monotonic):
        original_backup(original, target, deadline=deadline, monotonic=monotonic)
        backups.append(target)
        if len(backups) == 1:
            target.run("UPDATE schema_contract SET reference_file=?", (binding,))

    monkeypatch.setattr(SqliteConnection, "backup_to", backup)
    opened = []

    def snapshot(path, *, industry_snapshot, sqlite_policy):
        opened.append(path)
        assert invalid == "copy_escape" and path == history and len(opened) == 1
        return SimpleNamespace(snapshot=object())

    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", snapshot)
    monkeypatch.setattr(bootstrap, "ReadIndustrySnapshot", lambda *_args: CurrentIndustrySnapshotFixture(("600001",)))
    monkeypatch.setattr("scripts.training.jobs.training_alignment.shutil.disk_usage", lambda _path: SimpleNamespace(free=2**40))
    destination = tmp_path / "copy"
    before = paths[0].read_bytes()
    with pytest.raises(ValueError, match="training_history_copy_reference_invalid"):
        PrepareTrainingHistoryCopy(history, destination, timeout_seconds=30, sqlite_policy=SqlitePolicyFixture())
    assert not (destination / "copy-summary.json").exists()
    assert paths[0].read_bytes() == before
    assert opened == ([history] if invalid == "copy_escape" else [])
    assert bool(backups) == (invalid == "copy_escape")


@pytest.mark.parametrize("receipt_state", ("valid", "missing", "corrupt"))
def test_real_history_copy_preserves_readable_publication_and_rejects_invalid_source_receipts(tmp_path, monkeypatch, receipt_state):
    from scripts.training.jobs.training_alignment import PrepareTrainingHistoryCopy
    from scripts.training.jobs.training_alignment import _published_input_signature
    from scripts.training.samples.training_read_comparison import PrepareTrainingHistory
    from scripts.training.samples.training_read_comparison import TrainingRowsHash
    from trader.infra.sqlite_publication import SqlitePublicationError
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader.bootstrap import ReadIndustrySnapshot
    from zoneinfo import ZoneInfo

    policy = SqlitePolicyFixture()
    history = PrepareTrainingHistory(tmp_path / "source", size=2, sessions=40, sqlite_policy=policy)
    _publish_industry(history, policy)
    database = next(history.glob("prices_*.sqlite3"))
    receipt = database.with_name(database.name + ".sha256")
    if receipt_state == "missing":
        receipt.unlink()
    elif receipt_state == "corrupt":
        receipt.write_bytes(b"invalid")
    before = _published_input_signature(history)
    destination = tmp_path / "copy"
    monkeypatch.setattr("scripts.training.jobs.training_alignment.shutil.disk_usage", lambda _path: SimpleNamespace(free=2**40))
    if receipt_state != "valid":
        with pytest.raises(SqlitePublicationError, match="sqlite_publication_"):
            PrepareTrainingHistoryCopy(history, destination, timeout_seconds=30, sqlite_policy=policy)
        assert not (destination / "copy-summary.json").exists()
    else:
        result = PrepareTrainingHistoryCopy(history, destination, timeout_seconds=30, sqlite_policy=policy)
        industry = ReadIndustrySnapshot(history.parent / "reference", datetime.now(ZoneInfo("Asia/Shanghai")), policy)
        original = SQLiteHistoryTrainingInputSnapshot.open(history, industry_snapshot=industry, sqlite_policy=policy)
        copied = SQLiteHistoryTrainingInputSnapshot.open(destination / "history", industry_snapshot=industry, sqlite_policy=policy)
        dates = frozenset(original.snapshot.calendar.open_dates)
        assert copied.snapshot == original.snapshot
        assert TrainingRowsHash(copied.iter_training_rows(dates)) == TrainingRowsHash(original.iter_training_rows(dates))
        assert result["source_data_writes"] == 0
        assert result["source_files_unchanged"] is True
        assert result["publication_receipts"] == 1 + len(tuple(history.glob("prices_*.sqlite3")))
    assert _published_input_signature(history) == before


@pytest.mark.parametrize("failure", (False, True))
def test_training_resource_observer_stops_on_success_and_exception(tmp_path, failure):
    from scripts.training.resources.training_process_measurement import TrainingProcessMeasurement

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
    from scripts.training.resources.training_process_measurement import TrainingProcessMeasurement

    with TrainingProcessMeasurement(proc_root=tmp_path) as measurement:
        pass
    assert measurement.result["observed_peak_rss_bytes"] is None
    assert measurement.result["physical_read_bytes"] is None
    assert measurement.result["samples"] == 0
    assert measurement.result["sampling_failures"] == 2


@pytest.mark.parametrize("failure", (False, True))
def test_published_training_source_rejects_writes_and_restores_all_hooks(tmp_path, monkeypatch, failure):
    from scripts.training.jobs.training_source_guard import ReadOnlyTrainingSource
    from trader.training.infra.history import history_training_due
    from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

    source = tmp_path / "source/history"
    source.mkdir(parents=True)
    database = source / "catalog.sqlite3"
    with SqliteSession(database, sqlite_policy=SqlitePolicyFixture()) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample (value INTEGER)")
    before = database.read_bytes(), database.stat().st_mtime_ns
    queries = []

    def observe_due(query, *, sqlite_policy):
        queries.append(query)
        assert sqlite_policy is policy
        return None

    policy = SqlitePolicyFixture()
    monkeypatch.setattr(history_training_due, "EvaluateHistoryTrainingDue", observe_due)
    original_connect = sqlite3.connect
    query = history_training_due.HistoryTrainingDueQuery(
        source, tmp_path / "output", datetime.now(timezone.utc), V3_TRAINING_PROFILE,
        industry_snapshot=CurrentIndustrySnapshotFixture(("600001",)),
        expected_training_contract_hash="9" * 64,
    )
    with pytest.raises(InterruptedError) if failure else nullcontext():
        with ReadOnlyTrainingSource(source):
            with pytest.raises(ValueError, match="requires_read_only"), SqliteSession(database, sqlite_policy=policy):
                pytest.fail("writable source opened")
            with SqliteSession(database, SqliteConnectionOptions(read_only=True), sqlite_policy=policy) as connection:
                assert tuple(connection.run("SELECT count(*) FROM sample").require_one()) == (0,)
            with SqliteSession(tmp_path / "output.sqlite3", sqlite_policy=policy) as connection, SqliteTransaction(connection):
                connection.run("CREATE TABLE output (value INTEGER)")
            assert history_training_due.EvaluateHistoryTrainingDue(query, sqlite_policy=policy) is None
            if failure:
                raise InterruptedError("fixture cancellation")
    assert queries == [replace(query, persist=False)]
    assert history_training_due.EvaluateHistoryTrainingDue is observe_due
    assert sqlite3.connect is original_connect
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_published_input_signature_detects_changed_prices_and_rejects_symlinks(tmp_path):
    from scripts.training.jobs.training_alignment import _published_input_signature

    history = tmp_path / "history"
    history.mkdir()
    prices = history / "prices_2026.sqlite3"
    prices.write_bytes(b"original")
    before = _published_input_signature(history)
    prices.write_bytes(b"changed")
    assert before != _published_input_signature(history)
    changed = _published_input_signature(history)
    wal = prices.with_name(prices.name + "-wal")
    wal.write_bytes(b"")
    assert changed == _published_input_signature(history)
    wal.write_bytes(b"committed")
    assert changed != _published_input_signature(history)
    prices.unlink()
    prices.symlink_to(tmp_path / "foreign.sqlite3")
    (tmp_path / "foreign.sqlite3").write_bytes(b"unowned")
    with pytest.raises(ValueError, match="published_input_invalid"):
        _published_input_signature(history)


@pytest.mark.parametrize("suffix", ("", ".sha256"))
def test_published_input_signature_detects_same_size_rewrite_with_restored_mtime(tmp_path, suffix):
    from scripts.training.jobs.training_alignment import _published_input_signature

    history = tmp_path / "history"
    history.mkdir()
    database = history / "prices_2026.sqlite3"
    database.write_bytes(b"original")
    receipt = database.with_name(database.name + ".sha256")
    receipt.write_bytes(b"original")
    path = database.with_name(database.name + suffix)
    before = _published_input_signature(history)
    stamp = path.stat()
    deadline = time.monotonic() + 1.0
    while True:
        # Filesystems may update ctime only once per clock tick.
        time.sleep(0.01)
        path.write_bytes(b"modified")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        if path.stat().st_ctime_ns != stamp.st_ctime_ns:
            break
        if time.monotonic() >= deadline:
            pytest.skip("filesystem does not expose write-time ctime changes")
    assert path.stat().st_size == stamp.st_size
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns
    assert before != _published_input_signature(history)


@pytest.mark.parametrize("kind", ("symlink", "directory"))
def test_published_input_signature_rejects_unowned_publication_receipts(tmp_path, kind):
    from scripts.training.jobs.training_alignment import _published_input_signature

    history = tmp_path / "history"
    history.mkdir()
    database = history / "prices_2026.sqlite3"
    database.write_bytes(b"original")
    receipt = database.with_name(database.name + ".sha256")
    if kind == "symlink":
        protected = tmp_path / "protected"
        protected.write_bytes(b"keep")
        receipt.symlink_to(protected)
    else:
        receipt.mkdir()
    with pytest.raises(ValueError, match="published_input_invalid"):
        _published_input_signature(history)
    assert receipt.exists() and database.read_bytes() == b"original"


def test_training_resource_observer_reports_filesystem_space_without_claiming_owned_bytes(tmp_path, monkeypatch):
    from scripts.training.resources import training_process_measurement

    (tmp_path / "status").write_text("VmRSS: 1024 kB\nThreads: 4\n")
    (tmp_path / "io").write_text("read_bytes: 10\nwrite_bytes: 20\n")
    available = 1000

    def disk_usage(path):
        assert path == tmp_path
        return SimpleNamespace(free=available)

    monkeypatch.setattr(training_process_measurement.shutil, "disk_usage", disk_usage)
    with training_process_measurement.TrainingProcessMeasurement(proc_root=tmp_path, disk_root=tmp_path) as measurement:
        available = 500
    assert measurement.result["filesystem_free_bytes_at_start"] == 1000
    assert measurement.result["filesystem_minimum_free_bytes"] == 500
    assert "other_processes" in measurement.result["filesystem_measurement_scope"]


@pytest.mark.parametrize("computation_candidate", ("baseline", "combined"))
def test_real_published_history_training_never_persists_due_observations(tmp_path, monkeypatch, computation_candidate):
    from scripts.training.jobs import training_alignment
    from scripts.training.samples.training_read_comparison import PrepareTrainingHistory
    from trader.infra.settings import LoadRuntimeSettings
    from trader.infra.settings.data_paths import DataPaths
    from trader.training.infra import selected_training
    from trader.training.infra.training_settings import LoadProfileTrainingSettings

    project = Path(__file__).resolve().parents[4]
    runtime = replace(LoadRuntimeSettings(project / "config/runtime.json"), data_paths=DataPaths(tmp_path / "source"))
    history = PrepareTrainingHistory(tmp_path / "source", size=4, sessions=2000, sqlite_policy=SqlitePolicyFixture())
    _publish_industry(history, SqlitePolicyFixture())
    settings = LoadProfileTrainingSettings(project, "v3")
    settings = replace(settings, fitting=replace(settings.fitting, minimum_industry_training_rows=10))
    monkeypatch.setattr(selected_training, "LoadProfileTrainingSettings", lambda *_args: settings)
    monkeypatch.setattr("trader.infra.settings.LoadRuntimeSettings", lambda _path: runtime)
    # This checks file protection and report boundaries; host-pressure guards have dedicated tests.
    monkeypatch.setattr("trader.training.infra.memory_admission.FittingMemoryCapacity", lambda _settings: 512 * 1024**2)
    before = training_alignment._published_input_signature(history)
    report = training_alignment._probe(
        history,
        tmp_path / "output",
        ("v3",),
        sqlite_policy=SqlitePolicyFixture(),
        published_history_read_only=True,
        sample_cache_transfer="hardlink",
        computation_candidate=computation_candidate,
    )
    assert report["status"] == "passed", report
    summary = report["summary"]
    assert summary["computation_candidate"]["name"] == computation_candidate
    if computation_candidate == "combined":
        assert summary["computation_candidate"]["indexed_anchor_calls"] > 0
        assert summary["computation_candidate"]["schema_requests"] > summary["computation_candidate"]["schema_computations"] > 0
    assert summary["source_files_unchanged"] and summary["source_data_writes"] == 0
    assert summary["input_mode"] == "published_history_read_only"
    assert summary["repeat"]["execution"]["history_scan_passes"] == 0
    assert summary["sample_reuse"]["run"]["execution"]["history_scan_passes"] == 0
    assert before == training_alignment._published_input_signature(history)
    checkpoints = tmp_path / "output/workload-checkpoints"
    assert {path.name for path in checkpoints.iterdir()} == {
        "initial.json", "all_artifacts_reuse.json", "sample_preparation.json", "sample_refit.json", "sample_reuse.json",
    }
    saved = {path.stem: json.loads(path.read_text()) for path in checkpoints.iterdir()}
    assert all(item["audit_state"] == "pending_final_source_and_reuse_audit" for item in saved.values())
    assert all(item["performance_acceptance"] is item["production_authority"] is False for item in saved.values())
    assert saved["initial"]["result"]["execution"]["history_scan_passes"] == 1
    assert saved["all_artifacts_reuse"]["result"]["execution"]["history_scan_passes"] == 0
    assert saved["sample_preparation"]["result"]["fault_injected"] is True
    assert saved["sample_refit"]["result"]["execution"]["history_scan_passes"] == 0
    assert not saved["sample_reuse"]["result"]["failures"]


@pytest.mark.parametrize("interrupt_at", ("all_artifacts_reuse", "sample_reuse"))
def test_alignment_keeps_completed_workload_checkpoints_after_later_interruption(tmp_path, monkeypatch, interrupt_at):
    from scripts.training.jobs import training_alignment
    from trader.infra import settings
    from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
    from trader import bootstrap

    project = Path(__file__).resolve().parents[4]
    source = SimpleNamespace(snapshot=SimpleNamespace(snapshot_hash="a" * 64, training_input_hash="b" * 64,
                                                     training_codes=("600001",)))
    monkeypatch.setattr(SQLiteHistoryTrainingInputSnapshot, "open", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(bootstrap, "ReadIndustrySnapshot", lambda *_args: CurrentIndustrySnapshotFixture(("600001",)))
    monkeypatch.setattr(settings, "LoadRuntimeSettings", lambda _path: SimpleNamespace(
        project_root=project,
        data_paths=SimpleNamespace(root=tmp_path / "active", history=tmp_path / "active/history"),
    ))
    runs = 0

    def train_once(*_args, **_kwargs):
        nonlocal runs
        runs += 1
        if runs == 2 and interrupt_at == "all_artifacts_reuse":
            raise KeyboardInterrupt
        return {"complete": True, "seconds": 1.0, "resources": {"cpu_seconds": 0.5}, "run": runs}

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(training_alignment, "_train_once", train_once)
    monkeypatch.setattr(training_alignment, "_sample_reuse", interrupt)
    work = tmp_path / "output"
    with pytest.raises(KeyboardInterrupt):
        _probe(tmp_path / "history", work, ("v1",), sqlite_policy=SqlitePolicyFixture())
    saved = json.loads((work / "workload-checkpoints/initial.json").read_text())
    assert saved["result"] == {"complete": True, "seconds": 1.0, "resources": {"cpu_seconds": 0.5}, "run": 1}
    assert saved["audit_state"] == "pending_final_source_and_reuse_audit"
    assert saved["performance_acceptance"] is saved["production_authority"] is False
    assert (work / "workload-checkpoints/all_artifacts_reuse.json").exists() is (interrupt_at == "sample_reuse")
    assert not (work / "workload-checkpoints/sample_reuse.json").exists()


def test_first_workload_checkpoint_syncs_its_new_directory_entry(tmp_path, monkeypatch):
    from scripts.training.jobs import training_alignment
    from trader.infra.atomic_files import json as atomic_json

    work = tmp_path / "output"
    work.mkdir()
    synced = []
    original = atomic_json.SyncDirectory

    def sync(path):
        synced.append(path)
        original(path)

    monkeypatch.setattr(atomic_json, "SyncDirectory", sync)
    training_alignment._write_workload_checkpoint(work, "initial", {"complete": False})
    assert work in synced
    assert work / "workload-checkpoints" in synced


@pytest.mark.parametrize("foreign", ("directory_symlink", "directory_file", "existing_report", "report_symlink"))
def test_workload_checkpoint_preserves_existing_objects_and_rejects_symlinks(tmp_path, foreign):
    from scripts.training.jobs import training_alignment

    work = tmp_path / "output"
    work.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    sentinel = protected / "keep"
    sentinel.write_text("original")
    directory = work / "workload-checkpoints"
    if foreign == "directory_symlink":
        directory.symlink_to(protected, target_is_directory=True)
    elif foreign == "directory_file":
        directory.write_text("original")
    else:
        directory.mkdir()
        if foreign == "existing_report":
            (directory / "initial.json").write_text("original")
        else:
            (directory / "initial.json").symlink_to(sentinel)
    with pytest.raises((ValueError, FileExistsError), match="training_workload_checkpoint_"):
        training_alignment._write_workload_checkpoint(work, "initial", {"complete": True})
    assert sentinel.read_text() == "original"
    assert tuple(path.name for path in protected.iterdir()) == ("keep",)
    if foreign == "existing_report":
        assert (directory / "initial.json").read_text() == "original"
