"""Field materialization preserves input values and never certifies legacy fields."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.company import company_field_migration as migration
from scripts.download.company import company_resume
from trader.download.infra.company_facts_protocol import CompanyExtractionHash, EncodeCompanyTime
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.infra.settings import LoadStrategySettings
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnection, SqliteSchema, SqliteSession
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import PublishSqliteFile


_AT = datetime(2026, 11, 6, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_IO = SqliteIoPolicy(1.0, 128, 128, "PASSIVE")


@pytest.fixture
def policy():
    return LoadStrategySettings(PROJECT_ROOT / "config/strategy.json").company_scoring.events


def _source(tmp_path, policy):
    source = tmp_path / "input" / "company_facts.sqlite3"
    source.parent.mkdir()
    # A previously published fixture retains an obsolete numbering column.
    statements = tuple(
        statement.replace("CREATE TABLE company_publication (",
                          "CREATE TABLE company_publication (schema_version INTEGER NOT NULL CHECK(schema_version=3),")
        for statement in COMPANY_FACTS_SCHEMA.statements
        if not statement.startswith("CREATE TABLE company_field_")
    )
    schema = SqliteSchema(statements)
    at = EncodeCompanyTime(_AT)
    extraction = CompanyExtractionHash(policy)
    with SqliteSession(source) as connection:
        EnsureSqliteSchema(connection, schema, allow_create=True)
        connection.run(
            "INSERT INTO company_facts(code,finance_state,pledge_state,unlock_state,pending_docs,announcement_state,"
            "checked_at,published_at) VALUES('600001',0,0,0,0,1,?,?)", (at, at),
        )
        connection.run(
            "INSERT INTO company_positive_event(code,event_key,event_type,subject_type,status,first_disclosed_at,"
            "last_disclosed_at,level,actual_pct,repurchase_purpose) VALUES('600001','buy-one',1,1,2,?,?,0,0.1,2)",
            (at, at),
        )
        connection.run("INSERT INTO company_evidence_ref VALUES(17,1,'source-one')")
        positive_id = connection.run("SELECT id FROM company_positive_event").require_one()[0]
        connection.run("INSERT INTO company_positive_evidence VALUES(?,17,0)", (positive_id,))
        connection.run_many("INSERT INTO company_input_receipt VALUES(?,?,?,?,?,?,?,?,?)", (
            ("cninfo", "source-one", "600001", "record-one", extraction, b"b" * 32, at, "valid", "legacy-input-policy"),
            ("cninfo", "source-one", "600001", "record-one", b"f" * 32, b"b" * 32, at, "field_attempted", "old-field-policy"),
        ))
        connection.run("INSERT INTO company_publication(schema_version,singleton,extraction_hash,content_hash,published_at) "
                       "VALUES(3,1,?,?,?)", (extraction, b"s" * 32, at))
    PublishSqliteFile(source, sqlite_policy=_IO)
    return source


def _run(source, directory, policy, **changes):
    arguments = {"budget_seconds": 30.0, "sqlite_policy": _IO, "event_policy": policy, "protected_roots": ()}
    arguments.update(changes)
    return migration.AuditCompanyFieldMigration(source, directory, **arguments)


@pytest.mark.parametrize("provide_missing_collection", (False, True))
def test_migration_entrypoint_needs_only_facts_and_never_opens_a_collection(
    tmp_path, policy, monkeypatch, capsys, provide_missing_collection,
):
    source = _source(tmp_path, policy)
    before = source.read_bytes(), Path(f"{source}.sha256").read_bytes()
    directory = tmp_path / "entrypoint-migration"
    missing = tmp_path / "missing-collection.sqlite3"
    arguments = [
        "company-resume", "--field-migration-only", "--facts-database", str(source),
        "--directory", str(directory), "--budget-seconds", "30",
    ]
    if provide_missing_collection:
        arguments.extend(("--database", str(missing)))
    monkeypatch.setattr("sys.argv", arguments)
    monkeypatch.setattr(company_resume, "OpenExternalHttp", lambda: pytest.fail("migration must remain local"))

    assert company_resume.Main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and report["target_reader_verified"]
    assert report["source_data_writes"] == 0 and not report["production_authorized"]
    target = directory / "company_facts.sqlite3"
    reader = SQLiteCompanyEvidenceReader(target, event_policy=policy, sqlite_policy=_IO)
    assert reader.read_fields(("600001",)).publication_hash == report["target_publication_hash"]
    assert (source.read_bytes(), Path(f"{source}.sha256").read_bytes()) == before
    assert not missing.exists()


def test_rehearsal_preserves_all_old_values_and_receipts_and_only_adds_unverified_fields(tmp_path, policy):
    source = _source(tmp_path, policy)
    before = source.read_bytes(), Path(f"{source}.sha256").read_bytes()
    with SqliteSession(source) as connection:
        old_receipts = tuple(tuple(row) for row in connection.run("SELECT * FROM company_input_receipt ORDER BY extraction_hash"))
        old_publication = tuple(connection.run("SELECT * FROM company_publication").require_one())
    directory = tmp_path / "rehearsal"
    report = _run(source, directory, policy)
    assert report["status"] == "passed" and report["source_input_unchanged"]
    assert report["read_only_source"] and report["isolated_copy"] and not report["production_authorized"]
    assert report["source_data_writes"] == 0 and report["business_data_unchanged"] and report["original_receipts_unchanged"]
    assert report["target_reader_verified"] and "target_schema" not in report and "source_schema" not in report
    assert report["field_states"] == {"present_unverified": 2}
    assert report["fields"] == 2 and not report["field_correctness_verified"]
    assert report["source_file_publication_verified"] and report["source_backup_generation_verified"]
    assert len(report["preserved_business_tables"]) == 12
    target = directory / "company_facts.sqlite3"
    assert Path(f"{target}.sha256").is_file()
    with SqliteSession(target) as connection:
        assert tuple(tuple(row) for row in connection.run("SELECT * FROM company_input_receipt ORDER BY extraction_hash")) == old_receipts
        publication = tuple(connection.run("SELECT * FROM company_publication").require_one())
        assert publication[1] == old_publication[2] and publication[3] == old_publication[4]
        assert publication[2] != old_publication[3]
        assert connection.run("SELECT actual_pct,repurchase_purpose FROM company_positive_event").require_one()[:] == (0.1, 2)
        assert connection.run("SELECT COUNT(*) FROM company_field_evidence").require_one()[0] == 0
    with SqliteSession(directory / "source.sqlite3") as backup:
        assert tuple(backup.run("SELECT * FROM company_publication").require_one()) == old_publication
    reader = SQLiteCompanyEvidenceReader(target, event_policy=policy, sqlite_policy=_IO)
    assert reader.read_fields(("600001",)).publication_hash == report["target_publication_hash"]
    assert len(reader.read(("600001",))) == 1
    assert (source.read_bytes(), Path(f"{source}.sha256").read_bytes()) == before
    previous_report = (directory / "field-migration.json").read_bytes()
    with pytest.raises(ValueError, match="new_directory_required"):
        _run(source, directory, policy)
    assert (directory / "field-migration.json").read_bytes() == previous_report


@pytest.mark.parametrize("table", ("company_positive_evidence", "company_input_receipt"))
def test_missing_source_table_is_rejected_without_sealing_a_target(tmp_path, policy, table):
    source = _source(tmp_path, policy)
    with SqliteSession(source) as connection:
        connection.run(f"DROP TABLE {table}")
    PublishSqliteFile(source, sqlite_policy=_IO)
    before = source.read_bytes()
    directory = tmp_path / "rehearsal"
    report = _run(source, directory, policy)
    assert report["status"] == "failed" and report["failure_category"] == "ValueError"
    assert report["failure_code"] == "company_field_migration_source_tables_unsupported"
    assert (directory / "source.sqlite3").is_file()
    assert not (directory / "company_facts.sqlite3.sha256").exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("relation", ("nonempty", "symlink", "symlink_parent", "protected", "source_parent", "relative"))
def test_unsafe_or_existing_outputs_are_rejected_before_creating_artifacts(tmp_path, policy, relation):
    source = _source(tmp_path, policy)
    destination = tmp_path / "output"
    roots = ()
    if relation == "nonempty":
        destination.mkdir()
        (destination / "keep").write_text("keep", encoding="utf-8")
    elif relation == "symlink":
        real = tmp_path / "real"
        real.mkdir()
        destination.symlink_to(real, target_is_directory=True)
    elif relation == "symlink_parent":
        real = tmp_path / "real"
        real.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        destination = linked / "nested"
    elif relation == "protected":
        current = tmp_path / "current-data"
        destination = current / "replay"
        roots = (current,)
    elif relation == "source_parent":
        destination = source.parent
    else:
        destination = Path("relative-rehearsal")
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        _run(source, destination, policy, protected_roots=roots)
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


def test_copy_failure_rolls_back_target_and_preserves_external_failure_evidence(tmp_path, policy, monkeypatch):
    source = _source(tmp_path, policy)
    before = source.read_bytes()

    def interrupted(*_args):
        raise RuntimeError("synthetic-private-input-must-not-be-projected")

    monkeypatch.setattr(migration, "_baseline_fields", interrupted)
    directory = tmp_path / "rehearsal"
    report = _run(source, directory, policy)
    assert report["status"] == "failed" and report["failure_category"] == "RuntimeError"
    assert "synthetic-private-input" not in (directory / "field-migration.json").read_text()
    with SqliteSession(directory / "company_facts.sqlite3") as target:
        assert target.run("PRAGMA user_version").require_one()[0] == 0
        assert target.run("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").require_one()[0] == 0
    assert (directory / "source.sqlite3").is_file()
    assert not (directory / "company_facts.sqlite3.sha256").exists()
    assert source.read_bytes() == before


def test_source_symlink_is_rejected_without_creating_an_output(tmp_path, policy):
    source = _source(tmp_path, policy)
    linked = tmp_path / "linked-source.sqlite3"
    linked.symlink_to(source)
    destination = tmp_path / "rehearsal"
    with pytest.raises(ValueError, match="symlink_rejected"):
        _run(linked, destination, policy)
    assert not destination.exists()


def test_reader_policy_mismatch_never_rewrites_the_old_extraction_identity(tmp_path, policy):
    source = _source(tmp_path, policy)
    with SqliteSession(source) as connection:
        connection.run("UPDATE company_publication SET extraction_hash=?", (b"x" * 32,))
    PublishSqliteFile(source, sqlite_policy=_IO)
    before = source.read_bytes()
    destination = tmp_path / "rehearsal"
    report = _run(source, destination, policy)
    assert report["status"] == "failed" and report["failure_code"] == "company_field_migration_reader_policy_mismatch"
    assert report["source_extraction_hash"] == (b"x" * 32).hex()
    assert report.get("target_reader_verified") is not True
    assert source.read_bytes() == before


def test_source_foreign_key_failure_is_not_copied_into_a_published_target(tmp_path, policy):
    source = _source(tmp_path, policy)
    with SqliteSession(source) as connection:
        connection.run("DELETE FROM company_evidence_ref")
    PublishSqliteFile(source, sqlite_policy=_IO)
    before = source.read_bytes()
    destination = tmp_path / "rehearsal"
    report = _run(source, destination, policy)
    assert report["status"] == "failed" and report["failure_code"] == "company_field_migration_foreign_keys_failed"
    assert not (destination / "company_facts.sqlite3.sha256").exists()
    assert source.read_bytes() == before


@pytest.mark.parametrize("budget", (True, 0, -1, float("nan"), float("inf"), 10801))
def test_invalid_budget_is_rejected_before_creating_an_output(tmp_path, policy, budget):
    source = _source(tmp_path, policy)
    destination = tmp_path / "rehearsal"
    with pytest.raises(ValueError, match="budget_invalid"):
        _run(source, destination, policy, budget_seconds=budget)
    assert not destination.exists()


def test_budget_expiry_preserves_the_source_and_bounded_failure_report(tmp_path, policy, monkeypatch):
    source = _source(tmp_path, policy)
    before = source.read_bytes()
    ticks = iter((0.0, 100.0, 100.0))
    monkeypatch.setattr(migration.time, "monotonic", lambda: next(ticks))
    directory = tmp_path / "rehearsal"
    report = _run(source, directory, policy, budget_seconds=1.0)
    assert report["status"] == "failed" and report["deadline_exceeded"]
    assert report["failure_category"] == "TimeoutError"
    assert source.read_bytes() == before


@pytest.mark.parametrize("receipt_state", ("missing", "invalid"))
def test_unpublished_source_is_rejected_before_backup_and_never_resigned(tmp_path, policy, receipt_state):
    source = _source(tmp_path, policy)
    receipt = Path(f"{source}.sha256")
    if receipt_state == "missing":
        receipt.unlink()
    else:
        receipt.write_text("invalid-publication-receipt", encoding="utf-8")
    source_bytes = source.read_bytes()
    receipt_bytes = receipt.read_bytes() if receipt.exists() else None
    directory = tmp_path / "rehearsal"
    report = _run(source, directory, policy)
    assert report["status"] == "failed" and report["failure_category"] == "SqlitePublicationError"
    assert report["failure_code"] == (
        "sqlite_publication_file_missing" if receipt_state == "missing" else "sqlite_publication_receipt_invalid"
    )
    assert not (directory / "source.sqlite3").exists()
    assert not (directory / "company_facts.sqlite3").exists()
    assert source.read_bytes() == source_bytes
    assert (receipt.read_bytes() if receipt.exists() else None) == receipt_bytes


def test_source_commit_during_backup_invalidates_the_round_without_stopping_the_writer(tmp_path, policy, monkeypatch):
    source = _source(tmp_path, policy)
    original = SqliteConnection.backup_to

    def concurrent_commit(connection, target, *, deadline, monotonic):
        original(connection, target, deadline=deadline, monotonic=monotonic)
        # This is a distinct source writer, not an operation the rehearsal owns.
        with SqliteSession(source) as writer:
            writer.run("UPDATE company_positive_event SET actual_pct=0.2")
            writer.run("UPDATE company_publication SET content_hash=?", (b"n" * 32,))
        PublishSqliteFile(source, sqlite_policy=_IO)

    monkeypatch.setattr(SqliteConnection, "backup_to", concurrent_commit)
    directory = tmp_path / "rehearsal"
    report = _run(source, directory, policy)
    assert report["status"] == "failed" and report["source_changed_during_backup"]
    assert report["failure_code"] == "company_field_migration_source_changed_during_backup"
    assert report["source_data_writes"] == 0
    assert (directory / "source.sqlite3").exists()
    assert not (directory / "company_facts.sqlite3.sha256").exists()
    with SqliteSession(source) as connection:
        assert connection.run("SELECT actual_pct FROM company_positive_event").require_one()[0] == 0.2
