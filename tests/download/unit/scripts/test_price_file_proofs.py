"""Annual digest reuse does not rehash unchanged data or borrow field results."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.core.catalog import SelectionArguments
from scripts.core.cli import Main as ToolsMain
from scripts.download.history import history_publication_audit as audit
from scripts.download.history.price_file_proofs import PriceAuditFileProofs, PriceAuditProofPath
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments
from trader.infra import sqlite_publication
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction


def _seed(root: Path, year: int) -> Path:
    root.mkdir(exist_ok=True)
    path = root / f"prices_{year}.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE prices(price REAL)")
        connection.run("INSERT INTO prices VALUES(10)")
    sqlite_publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
    return path


def test_only_changed_year_is_rehashed_across_new_cache_instances(tmp_path, monkeypatch):
    first, second = (_seed(tmp_path / "history", year) for year in (2025, 2026))
    cache_path = tmp_path / "hashes.json"
    cache = PriceAuditFileProofs(cache_path, lambda: None)
    for path in (first, second):
        cache.verify(path, SqlitePolicyFixture())
    original = sqlite_publication._digest
    calls = []

    def tracked(path, size, check):
        calls.append(path)
        return original(path, size, check)

    monkeypatch.setattr(sqlite_publication, "_digest", tracked)
    resumed = PriceAuditFileProofs(cache_path, lambda: None)
    for path in (first, second):
        resumed.verify(path, SqlitePolicyFixture())
    assert calls == [] and resumed.reused_files == 2
    assert "schema_version" not in json.loads(cache_path.read_text())

    with SqliteSession(second) as connection, SqliteTransaction(connection):
        connection.run("UPDATE prices SET price=11")
    sqlite_publication.PublishSqliteFile(second, sqlite_policy=SqlitePolicyFixture())
    calls.clear()
    changed = PriceAuditFileProofs(cache_path, lambda: None)
    for path in (first, second):
        changed.verify(path, SqlitePolicyFixture())
    assert calls == [second]


def test_completed_year_proof_survives_interruption_before_the_next_year(tmp_path, monkeypatch):
    first, second = (_seed(tmp_path / "history", year) for year in (2025, 2026))
    cache_path = tmp_path / "hashes.json"
    stopped = False

    def check():
        if stopped:
            raise audit.PriceAuditInterrupted("budget_exhausted")

    cache = PriceAuditFileProofs(cache_path, check)
    cache.verify(first, SqlitePolicyFixture())
    stopped = True
    with pytest.raises(audit.PriceAuditInterrupted):
        cache.verify(second, SqlitePolicyFixture())

    def forbidden(*_args):
        pytest.fail("a completed year's body must not be read again")

    monkeypatch.setattr(sqlite_publication, "_digest", forbidden)
    PriceAuditFileProofs(cache_path, lambda: None).verify(first, SqlitePolicyFixture())


def test_generic_digest_checks_change_time_even_if_size_and_mtime_are_restored(tmp_path):
    source = tmp_path / "configuration.json"
    source.write_text("a" * 64)
    cache_path = tmp_path / "hashes.json"
    calls = []

    def compute():
        calls.append(source)
        return hashlib.sha256(source.read_bytes()).hexdigest()

    cache = PriceAuditFileProofs(cache_path, lambda: None)
    before = cache.digest(source, compute)
    cache.flush()
    assert PriceAuditFileProofs(cache_path, lambda: None).digest(source, compute) == before
    assert len(calls) == 1
    source.write_text("a" * 32 + "b" * 32)
    os.utime(source, ns=(source.stat().st_atime_ns, before.stamp.modified_ns))
    after = PriceAuditFileProofs(cache_path, lambda: None).digest(source, compute)
    assert len(calls) == 2 and after.sha256 != before.sha256


def test_corrupt_hash_cache_fails_without_silently_rehashing_sources(tmp_path, monkeypatch):
    path = _seed(tmp_path / "history", 2026)
    cache_path = tmp_path / "hashes.json"
    PriceAuditFileProofs(cache_path, lambda: None).verify(path, SqlitePolicyFixture())
    content = json.loads(cache_path.read_text())
    content["content_hash"] = "0" * 64
    cache_path.write_text(json.dumps(content))

    def forbidden(*_args):
        pytest.fail("invalid cache must not trigger a hidden full rehash")

    monkeypatch.setattr(sqlite_publication, "_digest", forbidden)
    with pytest.raises(ValueError, match="price_audit_hash_cache_invalid"):
        PriceAuditFileProofs(cache_path, lambda: None)


def test_hash_year_needs_no_catalog_and_does_not_start_field_scans(tmp_path, monkeypatch):
    root = tmp_path / "history"
    _seed(root, 2026)
    checkpoint = tmp_path / "checkpoint.json"

    def forbidden(*_args):
        pytest.fail("one-year hash checks must not load history metadata or price rows")

    monkeypatch.setattr(audit, "_history_plan", forbidden)
    monkeypatch.setattr(audit, "_scan_unit", forbidden)
    first = audit.AuditHistoryYearHash(
        root, 2026, checkpoint=checkpoint, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(5),
    )
    monkeypatch.setattr(sqlite_publication, "_digest", forbidden)
    second = audit.AuditHistoryYearHash(
        root, 2026, checkpoint=checkpoint, sqlite_policy=SqlitePolicyFixture(), budget=audit.PriceAuditBudget(5),
    )
    assert first["summary"]["database_hash_reused"] is False
    assert second["summary"]["database_hash_reused"] is True
    assert first["summary"]["database_sha256"] == second["summary"]["database_sha256"]
    assert not checkpoint.exists() and PriceAuditProofPath(checkpoint).is_file()


def test_hash_year_cli_wires_single_year_and_rejects_hash_cache_report_collision(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    arguments = [*SelectionArguments("history-publication"), "--hash-year", "2026", "--audit-checkpoint", str(checkpoint),
                 "--audit-budget-seconds", "5", "--command-timeout-seconds", "15"]
    options = ParseBusinessOptions(ParseToolArguments(arguments))
    command, = BuildToolCommands(options)
    assert command.argv[command.argv.index("--hash-year") + 1] == "2026"
    assert "--price-field-audit" not in command.argv
    with pytest.raises(SystemExit):
        ParseBusinessOptions(ParseToolArguments([*arguments, "--report", str(PriceAuditProofPath(checkpoint))]))


def test_hash_year_default_budget_is_short_without_changing_deep_audit_defaults(tmp_path):
    arguments = [*SelectionArguments("history-publication"), "--hash-year", "2026", "--audit-checkpoint", str(tmp_path / "checkpoint.json")]
    command, = BuildToolCommands(ParseBusinessOptions(ParseToolArguments(arguments)))
    assert command.timeout_seconds == 15
    assert command.argv[command.argv.index("--audit-budget-seconds") + 1] == "5.0"


def test_wal_digest_from_publication_is_not_computed_twice(tmp_path):
    path = tmp_path / "prices.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL")) as writer:
        with SqliteTransaction(writer):
            writer.run("CREATE TABLE prices(price REAL)")
            writer.run("INSERT INTO prices VALUES(10)")
        sqlite_publication.PublishSqliteFile(path, sqlite_policy=SqlitePolicyFixture())
        cache = PriceAuditFileProofs(tmp_path / "hashes.json", lambda: None)
        proof = cache.verify(path, SqlitePolicyFixture())

        def forbidden():
            pytest.fail("publication verification already hashed this WAL")

        digest = cache.digest(Path(str(path) + "-wal"), forbidden)
        assert digest.sha256 == proof.fingerprint.wal_sha256


def test_public_hash_year_entry_reuses_the_external_cache_without_catalog(tmp_path, capsys):
    root = tmp_path / "history"
    _seed(root, 2026)
    arguments = [*SelectionArguments("history-publication"), "--history-root", str(root), "--hash-year", "2026",
                 "--audit-checkpoint", str(tmp_path / "checkpoint.json")]
    assert ToolsMain(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert ToolsMain(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["checks"][0]["summary"]["database_hash_reused"] is False
    assert second["checks"][0]["summary"]["database_hash_reused"] is True
