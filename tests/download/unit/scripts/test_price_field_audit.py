"""Full price audits use published observations, bounded reads and exact resumable inputs."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.core.cli import Main as ToolsMain
from scripts.download.history import history_publication_audit as audit
from scripts.download.history.qfq_sqlite import AuditQfqPriceFields
from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.download.history.infra.history_sync import FakeSupplier, NOW
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.download.qfq import QFQ_CONFIGURATION, qfq_window
from tests.helpers.shared.configuration import PROJECT_ROOT, SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.download.infra.history_revision_codec import RevisionColumnValues, RevisionFromColumns
from trader.infra.sqlite_codec import EncodeDate, EncodeHash
from trader.infra.sqlite_publication import PublishSqliteFile


def _seed_history(root: Path, *, days: tuple[date, ...] | None = None) -> None:
    configuration = HistorySyncConfigurationFixture(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=root.parent / "reference",
    )
    selected = days if days is not None else (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert RunHistoryFixture(configuration, FakeSupplier(selected), clock=lambda: NOW).state == "completed"


def _edit(path: Path, sql: str, arguments: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(sql, arguments)
    PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())


def _history_report(root: Path, checkpoint: Path | None = None):
    return audit.AuditHistoryPriceFields(
        root, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30), checkpoint=checkpoint,
    )["summary"]["field_audit"]


def _seed_qfq(root: Path, *, closed_day: date | None = None) -> tuple[Path, Path]:
    path = root / "market" / "daily_window.sqlite3"
    reference_path = root / "reference" / "company_st.sqlite3"
    window = qfq_window("600001")
    window = replace(window, cells=tuple(cell for cell in window.cells if cell.trade_date != closed_day) if closed_day is not None else window.cells[-251:])
    dates = tuple(cell.trade_date for cell in window.cells)
    # The fixture includes calendar dates rather than exchange weekdays; both
    # published windows and the explicit local reference use the same dates.
    observed = datetime(2026, 9, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    reference = PreparedFixture(observed, open_dates=(*dates, observed.date()))
    SQLiteReferenceIndex(reference_path, sqlite_policy=SqlitePolicyFixture()).publish(reference, reference.st_evidence)
    primary = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(path, "v3", sqlite_policy=SqlitePolicyFixture())
    primary.replace_paired_windows(peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=dates, completed_day=dates[-1])
    return path, reference_path


def _advance_history_publication(root: Path) -> None:
    catalog = root / "catalog.sqlite3"
    before = HistoryControlIndex(catalog, sqlite_policy=SqlitePolicyFixture()).load_published_state()
    assert before is not None
    current = replace(before.snapshot, sequence=before.snapshot.sequence + 1)
    with sqlite3.connect(catalog) as connection:
        connection.execute("INSERT INTO history_snapshot VALUES(?,?,?,?,?,?,?)", (
            current.sequence, EncodeHash(current.content_hash), EncodeDate(current.data_cutoff), EncodeDate(current.label_cutoff),
            EncodeHash(current.calendar_hash), EncodeHash(current.universe_hash), EncodeHash(current.source_identity_hash),
        ))
        connection.execute("INSERT INTO snapshot_partition SELECT ?,calendar_year,calendar_month,relative_path,logical_hash,row_count,sync_sequence FROM snapshot_partition WHERE snapshot_id=?", (current.sequence, before.snapshot.sequence))
        connection.execute("UPDATE published_snapshot SET sync_sequence=?,snapshot_hash=?", (current.sequence, EncodeHash(current.content_hash)))
        connection.execute("UPDATE download_batch SET sync_sequence=?", (current.sequence,))
    PublishSqliteFile(catalog, sqlite_policy=SqlitePolicyFixture())


def _advance_reference(path: Path) -> None:
    index = SQLiteReferenceIndex(path, sqlite_policy=SqlitePolicyFixture())
    before = index.read()
    assert before is not None
    current = replace(before, official_universe=tuple(replace(security, name=security.name + " changed") for security in before.official_universe))
    index.publish(current, current.st_evidence)


def test_history_full_scan_counts_actual_rows_and_legal_qfq_nulls_without_writes(tmp_path):
    root = tmp_path / "history"
    _seed_history(root)
    sources = tuple(root.glob("*.sqlite3"))
    before = tuple((path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).digest()) for path in sources)

    fields = _history_report(root)

    assert fields["status"] == "passed" and fields["complete"] is True
    assert fields["actual_selected_rows"] == 6
    assert fields["sha256_validation"] == fields["metadata_sqlite_integrity"] == "passed"
    assert fields["units"][0]["sqlite_integrity"] == "passed"
    assert fields["units"][0]["declared_manifest_rows"] == 6
    assert fields["active_raw_auxiliary_null_values"] == 0
    qfq_preclose = next(item for item in fields["units"][0]["fields"] if item["column"] == "qfq_preclose")
    assert qfq_preclose["nulls"] == 6 and qfq_preclose["active_core_nulls"] == 0
    assert all(item["expected_stock_days"] == item["trading_days"] == 3 for item in fields["units"][0]["stock_date_coverage"])
    assert before == tuple((path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).digest()) for path in sources)


def test_history_field_audit_keeps_unknown_st_members_from_the_formal_publication(tmp_path):
    root = tmp_path / "history"
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class Supplier(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            return replace(context, st_evidence=tuple(
                replace(item, status="unknown") if item.code == "600002" else item for item in context.st_evidence
            ))

    configuration = HistorySyncConfigurationFixture(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=root.parent / "reference",
    )
    assert RunHistoryFixture(configuration, Supplier(days), clock=lambda: NOW).state == "completed"
    fields = _history_report(root)
    assert fields["complete"] is True and fields["actual_selected_rows"] == 6
    assert {row["code"] for row in fields["units"][0]["stock_date_coverage"]} == {"600001", "600002"}
    assert fields["unexplained_days"] == 0 and fields["invalid_values"] == 0


@pytest.mark.parametrize("source", ("catalog", "reference"))
def test_history_commit_between_metadata_and_sha_cannot_borrow_new_file_proof(tmp_path, monkeypatch, source):
    root = tmp_path / "history"
    _seed_history(root)
    original = audit._proofs

    def committed(plan, configuration, budget, policy):
        if source == "catalog":
            _advance_history_publication(root)
        else:
            _advance_reference(plan.files[1][0])
        return original(plan, configuration, budget, policy)

    monkeypatch.setattr(audit, "_proofs", committed)
    fields = _history_report(root)
    assert fields["complete"] is False and fields["status"] == "failed"
    assert fields["failure"] == "price_audit_metadata_source_changed"
    assert fields["actual_selected_rows"] == 0


def test_history_commit_after_scanning_keeps_counts_but_cannot_complete(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root)
    original = audit._scan_unit

    def committed(scan, budget, policy):
        original(scan, budget, policy)
        _advance_history_publication(root)

    monkeypatch.setattr(audit, "_scan_unit", committed)
    fields = _history_report(root)
    assert fields["failure"] == "price_audit_metadata_source_changed" and fields["complete"] is False
    assert fields["actual_selected_rows"] == 6 and fields["completed_units"] == 1


def test_no_wal_metadata_read_does_not_create_its_own_source_drift(tmp_path):
    root = tmp_path / "history"
    _seed_history(root)
    for database in root.glob("*.sqlite3"):
        wal = Path(str(database) + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0
        wal.unlink(missing_ok=True)
        Path(str(database) + "-shm").unlink(missing_ok=True)

    fields = _history_report(root)

    assert fields["complete"] is True and fields["status"] == "passed"
    assert not tuple(root.glob("*.sqlite3-wal"))
    assert not tuple(root.glob("*.sqlite3-shm"))


def test_history_scan_does_not_substitute_manifest_row_count(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root)
    plan = audit._history_plan(root, audit.PriceAuditBudget(30), SqlitePolicyFixture())
    declared = replace(plan, units=tuple(replace(unit, declared_rows=23) for unit in plan.units))
    monkeypatch.setattr(audit, "_history_plan", lambda *_args: declared)

    fields = _history_report(root)

    assert fields["actual_selected_rows"] == 6
    assert fields["units"][0]["declared_manifest_rows"] == 23


def test_history_formal_calendar_month_cannot_disappear_from_expected_coverage(tmp_path):
    root = tmp_path / "history"
    _seed_history(root, days=(date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2)))
    published = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_published_state()
    assert published is not None
    incomplete = replace(published.snapshot, partitions=tuple(part for part in published.snapshot.partitions if part.calendar_year == 2026))
    with sqlite3.connect(root / "catalog.sqlite3") as connection:
        connection.execute("DELETE FROM snapshot_partition WHERE snapshot_id=? AND calendar_year=2025", (incomplete.sequence,))
        connection.execute("UPDATE history_snapshot SET content_hash=? WHERE id=?", (EncodeHash(incomplete.content_hash), incomplete.sequence))
        connection.execute("UPDATE published_snapshot SET snapshot_hash=?", (EncodeHash(incomplete.content_hash),))
    PublishSqliteFile(root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())

    with pytest.raises(ValueError, match="partition_calendar_mismatch"):
        _history_report(root)


def test_history_raw_auxiliary_null_is_reported_without_zero_or_derived_values(tmp_path):
    root = tmp_path / "history"
    _seed_history(root)
    _edit(root / "prices_2026.sqlite3", "UPDATE daily_price_revision SET raw_preclose=NULL,raw_change_pct=NULL")

    fields = _history_report(root)

    assert fields["status"] == "degraded" and fields["complete"] is True
    assert fields["active_raw_auxiliary_null_values"] == 12
    assert fields["invalid_values"] == 0
    assert fields["all_active_raw_auxiliary_fields_present"] is False


@pytest.mark.parametrize("sql", (
    "UPDATE daily_price_revision SET raw_open=NULL",
    "UPDATE daily_price_revision SET qfq_amount=-1",
    "UPDATE daily_price_revision SET raw_high=raw_low-1",
    "UPDATE daily_price_revision SET qfq_preclose=1",
))
def test_history_core_and_semantic_field_failures_remain_visible(tmp_path, sql):
    root = tmp_path / "history"
    _seed_history(root)
    _edit(root / "prices_2026.sqlite3", sql)

    fields = _history_report(root)

    assert fields["status"] == "failed" and fields["complete"] is True
    assert fields["actual_selected_rows"] == 6 and fields["invalid_values"] > 0


def test_history_latest_selection_obeys_published_month_sequence(tmp_path):
    root = tmp_path / "history"
    _seed_history(root)
    annual = root / "prices_2026.sqlite3"
    # A later observation selects an unavailable revision and must not replace
    # the formal month sequence during this audit.
    _edit(annual, "INSERT INTO daily_price_observation SELECT trade_date,code,99,'unpublished' FROM daily_price_observation WHERE code='600001'")

    fields = _history_report(root)

    assert fields["actual_selected_rows"] == 6
    assert fields["missing_selected_revision_rows"] == 0


@pytest.mark.parametrize(("days", "extra_day", "expected_unexpected"), (
    ((date(2026, 9, 7), date(2026, 9, 9), date(2026, 9, 10)), date(2026, 9, 8), 1),
    ((date(2026, 8, 29), date(2026, 9, 1), date(2026, 9, 2)), date(2026, 8, 30), 1),
    ((date(2026, 9, 7), date(2026, 9, 9), date(2026, 9, 10)), date(2026, 9, 6), 0),
))
def test_history_closed_date_observation_is_invalid_only_inside_published_window(tmp_path, days, extra_day, expected_unexpected):
    root = tmp_path / "history"
    _seed_history(root, days=days)
    annual = root / "prices_2026.sqlite3"
    with sqlite3.connect(annual) as connection:
        row = connection.execute("SELECT * FROM daily_price_revision ORDER BY trade_date,code LIMIT 1").fetchone()
        revision = RevisionFromColumns(tuple(row))
        assert revision.cell.unadjusted is not None and revision.cell.qfq is not None
        cell = replace(
            revision.cell,
            trade_date=extra_day,
            unadjusted=replace(revision.cell.unadjusted, trade_date=extra_day),
            qfq=replace(revision.cell.qfq, trade_date=extra_day),
        )
        extra = replace(revision, cell=cell)
        values = (EncodeDate(extra_day), extra.code, extra.revision_id, extra.first_seen_sequence, extra.board, *RevisionColumnValues(extra), EncodeHash(extra.content_hash))
        connection.execute("INSERT INTO daily_price_revision VALUES(" + ",".join("?" for _ in values) + ")", values)
        connection.execute("INSERT INTO daily_price_observation VALUES(?,?,?,?)", (EncodeDate(extra_day), extra.code, extra.first_seen_sequence, extra.revision_id))
    PublishSqliteFile(annual, sqlite_policy=SqlitePolicyFixture())

    fields = _history_report(root)

    assert fields["unexpected_calendar_stock_days"] == expected_unexpected
    assert fields["actual_selected_rows"] == 6 + expected_unexpected
    assert sum(stock["expected_stock_days"] for unit in fields["units"] for stock in unit["stock_date_coverage"]) == 6
    assert fields["status"] == ("failed" if expected_unexpected else "passed")


def test_history_observations_are_extracted_once_not_queried_per_revision(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root)
    original = audit.PriceAuditBudget.connection
    original_connect = sqlite3.connect
    statements: list[str] = []
    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []

    class _MemoryConnection(sqlite3.Connection):
        def close(self) -> None:
            closed.append(self)
            super().close()

    def connect(database, *arguments, **keywords):
        if database == ":memory:":
            keywords["factory"] = _MemoryConnection
            connection = original_connect(database, *arguments, **keywords)
            opened.append(connection)
            return connection
        return original_connect(database, *arguments, **keywords)

    @contextmanager
    def traced(budget, path, policy):
        with original(budget, path, policy) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(audit.PriceAuditBudget, "connection", traced)
    monkeypatch.setattr(audit.sqlite3, "connect", connect)
    fields = _history_report(root)
    observation_queries = [statement for statement in statements if "FROM daily_price_observation" in statement]
    assert fields["actual_selected_rows"] == 6
    assert len(observation_queries) == 1
    assert "ORDER BY trade_date,code,sync_sequence" in observation_queries[0]
    assert len(opened) == 1 and opened == closed


def test_checkpoint_exact_inputs_reuse_only_completed_units(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root)
    checkpoint = tmp_path / "checkpoint.json"
    first = _history_report(root, checkpoint)
    assert first["completed_units"] == 1

    def unexpected(*_args):
        pytest.fail("an exactly matching completed annual scan must be reused")

    monkeypatch.setattr(audit, "_scan_unit", unexpected)
    resumed = _history_report(root, checkpoint)
    assert resumed["resumed_units"] == 1 and resumed["actual_selected_rows"] == 6


def test_checkpoint_rejects_changed_wal_existence(tmp_path):
    root = tmp_path / "history"
    _seed_history(root)
    checkpoint = tmp_path / "checkpoint.json"
    assert _history_report(root, checkpoint)["complete"] is True
    # Empty-WAL existence is part of the actual fingerprint, independently of
    # the production receipt's absent/empty normalization.
    wal = root / "prices_2026.sqlite3-wal"
    if wal.exists():
        wal.unlink()
    else:
        wal.touch()

    fields = _history_report(root, checkpoint)

    assert fields["complete"] is False
    assert fields["failure"] == "price_audit_checkpoint_fingerprint_mismatch"


def test_checkpoint_rejects_changed_configuration_bytes(tmp_path):
    root = tmp_path / "history"
    _seed_history(root)
    configuration = tmp_path / "config" / "audit.json"
    configuration.parent.mkdir()
    configuration.write_text('{"sample":1}', encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    first = audit.AuditHistoryPriceFields(root, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30), checkpoint=checkpoint, configuration=(configuration,))
    assert first["summary"]["field_audit"]["complete"] is True
    saved = json.loads(checkpoint.read_text())
    assert "schema_version" not in saved
    configuration.write_text('{"sample":2}', encoding="utf-8")

    changed = audit.AuditHistoryPriceFields(root, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30), checkpoint=checkpoint, configuration=(configuration,))

    assert changed["summary"]["field_audit"]["failure"] == "price_audit_checkpoint_fingerprint_mismatch"


def test_checkpoint_rejects_changed_source_digest(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root)
    checkpoint = tmp_path / "checkpoint.json"
    assert _history_report(root, checkpoint)["complete"] is True
    original = audit._hash_file

    def changed(path, budget):
        proof = original(path, budget)
        return replace(proof, sha256="0" * 64) if path == Path(audit.__file__) else proof

    monkeypatch.setattr(audit, "_hash_file", changed)
    assert _history_report(root, checkpoint)["failure"] == "price_audit_checkpoint_fingerprint_mismatch"


def test_expired_budget_checks_before_missing_source_and_preserves_partial_report(tmp_path):
    unit = audit.PriceAuditUnit("missing", tmp_path / "missing.sqlite3", (1,), (audit.PriceAuditSecurity("600001", 0, None),))
    plan = audit.PriceAuditPlan("fixture", (unit,), ((unit.path, False),))
    ticks = iter((0.0, 2.0))
    fields = audit.AuditPriceFields(plan, budget=audit.PriceAuditBudget(1, clock=lambda: next(ticks)), sqlite_policy=SqlitePolicyFixture())
    assert fields["complete"] is False and fields["failure"] == "price_audit_budget_exhausted"
    assert not unit.path.exists()


def test_cancellation_keeps_completed_unit_and_never_checkpoints_partial_unit(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root)
    checkpoint = tmp_path / "checkpoint.json"
    original = audit._scan_unit
    stopped = False

    def interrupt(scan, budget, policy):
        nonlocal stopped
        original(scan, budget, policy)
        stopped = True
        budget.check()

    monkeypatch.setattr(audit, "_scan_unit", interrupt)
    report = audit.AuditHistoryPriceFields(root, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30, cancelled=lambda: stopped), checkpoint=checkpoint)
    fields = report["summary"]["field_audit"]
    assert fields["complete"] is False and fields["failure"] == "price_audit_cancelled"
    assert fields["actual_selected_rows"] == 6 and fields["completed_units"] == 0
    assert fields["units"][0]["complete"] is False
    assert not checkpoint.exists()


def test_cancelled_second_year_resumes_only_after_completed_first_year(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed_history(root, days=(date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2)))
    checkpoint = tmp_path / "checkpoint.json"
    original = audit._scan_unit
    stopped = False
    calls = 0

    def interrupt(scan, budget, policy):
        nonlocal stopped, calls
        calls += 1
        if calls == 2:
            stopped = True
            budget.check()
        original(scan, budget, policy)

    monkeypatch.setattr(audit, "_scan_unit", interrupt)
    report = audit.AuditHistoryPriceFields(root, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30, cancelled=lambda: stopped), checkpoint=checkpoint)
    partial = report["summary"]["field_audit"]
    assert partial["complete"] is False and partial["completed_units"] == 1
    assert len(json.loads(checkpoint.read_text())["completed_units"]) == 1
    monkeypatch.setattr(audit, "_scan_unit", original)
    resumed = _history_report(root, checkpoint)
    assert resumed["complete"] is True and resumed["resumed_units"] == 1
    assert resumed["actual_selected_rows"] == 6


def test_sql_progress_cancels_only_this_audits_interruption(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fixture(value INTEGER)")
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 1

    budget = audit.PriceAuditBudget(30, cancelled=cancelled)
    with pytest.raises(audit.PriceAuditInterrupted, match="cancelled"), budget.connection(path, SqlitePolicyFixture()) as connection:
        connection.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000000) SELECT sum(x) FROM n").fetchone()
    with pytest.raises(sqlite3.OperationalError, match="no such column"), audit.PriceAuditBudget(30).connection(path, SqlitePolicyFixture()) as connection:
        connection.execute("SELECT missing_column FROM fixture")


def test_expected_stock_bitmaps_obey_same_cancellation_budget(tmp_path):
    unit = audit.PriceAuditUnit("fixture", tmp_path / "unused.sqlite3", (1, 2, 3), tuple(audit.PriceAuditSecurity(code, 0, None) for code in ("600001", "600002", "600003")))
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(audit.PriceAuditInterrupted, match="cancelled"):
        audit._UnitScan(unit, audit.PriceAuditBudget(30, cancelled=cancelled))
    assert calls == 2


@pytest.mark.parametrize("phase", ("encoding", "fsync"))
def test_checkpoint_encoding_and_fsync_check_same_cancellation_budget(tmp_path, monkeypatch, phase):
    root = tmp_path / "history"
    _seed_history(root)
    checkpoint = tmp_path / "checkpoint.json"
    stopped = False
    if phase == "encoding":
        original_dumps = json.dumps

        def dumps(value, *arguments, **keywords):
            nonlocal stopped
            payload = original_dumps(value, *arguments, **keywords)
            if isinstance(value, dict) and "completed_units" in value:
                stopped = True
            return payload

        monkeypatch.setattr(audit.json, "dumps", dumps)
    else:
        original_fsync = audit.os.fsync

        def fsync(descriptor):
            nonlocal stopped
            original_fsync(descriptor)
            stopped = True

        monkeypatch.setattr(audit.os, "fsync", fsync)
    report = audit.AuditHistoryPriceFields(root, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30, cancelled=lambda: stopped), checkpoint=checkpoint)
    fields = report["summary"]["field_audit"]
    assert fields["complete"] is False and fields["failure"] == "price_audit_cancelled"
    assert fields["completed_units"] == 1 and fields["actual_selected_rows"] == 6
    assert not checkpoint.exists() and not tuple(tmp_path.glob("checkpoint.json.*"))


def test_qfq_scan_validates_both_binding_hashes_and_fields(tmp_path):
    path, reference = _seed_qfq(tmp_path)
    report = AuditQfqPriceFields(path, reference_database=reference, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30))
    fields = report["summary"]["field_audit"]
    assert report["status"] == "passed" and fields["complete"] is True
    assert fields["actual_selected_rows"] == 251 + 61
    assert all(unit["binding_hash_mismatches"] == 0 for unit in fields["units"])
    assert fields["unexplained_days"] == fields["inferred_nontrading_days"] == 0


@pytest.mark.parametrize("source", ("database", "reference"))
def test_qfq_commit_between_metadata_and_sha_cannot_borrow_new_file_proof(tmp_path, monkeypatch, source):
    path, reference = _seed_qfq(tmp_path)
    original = audit._proofs

    def committed(plan, configuration, budget, policy):
        if source == "database":
            with sqlite3.connect(path) as connection:
                previous = connection.execute("SELECT publication_id FROM window_publication LIMIT 1").fetchone()[0]
                token = "0" * 32 if previous != "0" * 32 else "1" * 32
                connection.execute("UPDATE window_publication SET publication_id=?", (token,))
            SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture()).publish_file()
        else:
            _advance_reference(reference)
        return original(plan, configuration, budget, policy)

    monkeypatch.setattr(audit, "_proofs", committed)
    fields = AuditQfqPriceFields(path, reference_database=reference, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30))["summary"]["field_audit"]
    assert fields["complete"] is False and fields["failure"] == "price_audit_metadata_source_changed"
    assert fields["actual_selected_rows"] == 0


def test_qfq_binding_digest_includes_unexpected_closed_day_without_counting_coverage(tmp_path):
    closed_day = date(2025, 1, 11)
    path, reference = _seed_qfq(tmp_path, closed_day=closed_day)
    with sqlite3.connect(path) as connection:
        original = connection.execute("SELECT * FROM daily_price ORDER BY code,trade_date LIMIT 1").fetchone()
        extra = (original[0], EncodeDate(closed_day), *original[2:])
        connection.execute("INSERT INTO daily_price VALUES(" + ",".join("?" for _ in extra) + ")", extra)
    SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture()).publish_file()

    fields = AuditQfqPriceFields(path, reference_database=reference, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30))["summary"]["field_audit"]

    assert fields["status"] == "failed" and fields["complete"] is True
    assert fields["unexpected_calendar_stock_days"] == 1
    assert fields["binding_content_validation"] == "failed"
    assert fields["actual_selected_rows"] == 251 + 61 + 1
    assert sum(stock["expected_stock_days"] for unit in fields["units"] for stock in unit["stock_date_coverage"]) == 251 + 61
    assert fields["unexplained_days"] == 0


def test_qfq_missing_dates_keep_confirmed_inferred_and_unexplained_separate(tmp_path):
    path, reference = _seed_qfq(tmp_path)
    with sqlite3.connect(path) as connection:
        days = tuple(row[0] for row in connection.execute("SELECT trade_date FROM window_calendar WHERE profile='v2' ORDER BY trade_date DESC LIMIT 3"))
        for day in days:
            connection.execute("DELETE FROM daily_price WHERE trade_date=?", (day,))
        connection.executemany("INSERT INTO price_nontrading VALUES('600001',?,?,?, 'fixture')", (
            (days[0], "confirmed", EncodeDate(date(2026, 9, 10))),
            (days[1], "inferred", EncodeDate(date(2026, 9, 10))),
        ))
    SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture()).publish_file()

    fields = AuditQfqPriceFields(path, reference_database=reference, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(30))["summary"]["field_audit"]

    assert fields["confirmed_nontrading_days"] == 2
    assert fields["inferred_nontrading_days"] == 2
    assert fields["unexplained_days"] == 2
    assert fields["status"] == "failed"  # The unchanged formal binding hashes also expose the deletion.


@pytest.mark.parametrize("identity", ("history-publication", "qfq-sqlite"))
def test_deep_audit_cli_wires_budget_checkpoint_and_rejects_report_collision(tmp_path, identity):
    checkpoint = tmp_path / "checkpoint.json"
    arguments = [*SelectionArguments(identity), "--price-field-audit", "--audit-checkpoint", str(checkpoint), "--audit-budget-seconds", "20", "--command-timeout-seconds", "30"]
    options = ParseBusinessOptions(ParseToolArguments(arguments))
    command, = BuildToolCommands(options)
    assert "--price-field-audit" in command.argv and str(checkpoint) in command.argv
    assert command.argv[command.argv.index("--audit-budget-seconds") + 1] == "20.0"
    with pytest.raises(SystemExit):
        ParseToolArguments([*arguments, "--report", str(checkpoint)])


@pytest.mark.parametrize(("identity", "directory", "option"), (
    ("history-publication", "history", "--report"),
    ("history-publication", "reference", "--report"),
    ("history-publication", "reference", "--audit-checkpoint"),
    ("qfq-sqlite", "market", "--report"),
    ("qfq-sqlite", "reference", "--report"),
    ("qfq-sqlite", "market", "--audit-checkpoint"),
    ("qfq-sqlite", "config", "--report"),
))
@pytest.mark.parametrize("invalid_budget", (False, True))
def test_public_audit_rejects_new_output_in_external_sources_before_execution(tmp_path, monkeypatch, capsys, identity, directory, option, invalid_budget):
    configuration = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", configuration)
    history = tmp_path / "history"
    history.mkdir()
    external_reference = tmp_path / "custom-reference"
    external_reference.mkdir()
    with sqlite3.connect(history / "catalog.sqlite3") as connection:
        connection.execute("CREATE TABLE schema_contract(reference_file TEXT)")
        connection.execute("INSERT INTO schema_contract VALUES('../custom-reference/company_st.sqlite3')")
    market = tmp_path / "data" / "market"
    reference = market / "reference"
    reference.mkdir(parents=True)
    if directory == "history":
        source_directory = history
    elif directory == "reference":
        source_directory = external_reference if identity == "history-publication" else reference
    elif directory == "config":
        source_directory = configuration
    else:
        source_directory = market
    output = source_directory / "new-audit.json"
    arguments = [*SelectionArguments(identity), "--price-field-audit", "--runtime-config", str(configuration / "runtime.json"), option, str(output)]
    if invalid_budget:
        arguments.extend(("--audit-budget-seconds", "50", "--command-timeout-seconds", "30"))
    arguments.extend(["--history-root", str(history)] if identity == "history-publication" else ["--qfq-database", str(market / "daily_window.sqlite3")])

    def unexpected(*_arguments, **_keywords):
        pytest.fail("an unsafe output path must be rejected before executing diagnostics")

    monkeypatch.setattr("scripts.core.execution.RunDiagnostics", unexpected)
    with pytest.raises(SystemExit):
        ToolsMain(arguments)
    assert "separate" in capsys.readouterr().err
    assert not output.exists()


def test_repair_codes_require_external_work_root_and_wire_explicit_codes(tmp_path):
    selection = SelectionArguments("price-repair-audit")
    options = ParseBusinessOptions(ParseToolArguments([*selection, "--repair-codes", "300118", "600001", "--database-probe-directory", str(tmp_path / "probe")]))
    command, = BuildToolCommands(options)
    assert command.argv[command.argv.index("--codes") + 1:command.argv.index("--codes") + 3] == ("300118", "600001")
    with pytest.raises(ValueError):
        ParseBusinessOptions(ParseToolArguments([*selection, "--repair-codes", "300118"]))


@pytest.mark.parametrize("mode", ("ordinary", "codes", "profile"))
def test_repair_runtime_configuration_is_forwarded_once_for_every_mode(tmp_path, mode):
    configuration = tmp_path / "selected-config" / "runtime.json"
    arguments = [*SelectionArguments("price-repair-audit"), "--history-root", str(tmp_path / "history"), "--database-probe-directory", str(tmp_path / "probe"), "--runtime-config", str(configuration)]
    if mode == "codes":
        arguments.extend(("--repair-codes", "300118"))
    elif mode == "profile":
        arguments.extend(("--repair-profile-year", "2026", "--repair-profile-code", "300118"))
    command, = BuildToolCommands(ParseBusinessOptions(ParseToolArguments(arguments)))
    assert command.argv.count("--runtime-config") == 1
    assert command.argv[command.argv.index("--runtime-config") + 1] == str(configuration)
