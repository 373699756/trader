from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.migrate_runtime_data import MigrationError, inspect_layout, migrate
from scripts.runtime_migration import MergeDatabase
from tests.unit.infra.test_freeze_records import NOW, record
from tests.unit.infra.test_issuer_eligibility_index import _fact
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFactHash, ManualBlacklistFact
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "data").mkdir(parents=True)
    return source


def _legacy_freeze(source: Path) -> dict[str, bytes]:
    expected = {}
    for profile in ("v2", "v3"):
        root = source / "data/freeze" / profile
        root.mkdir(parents=True)
        with SqliteSession(root / "freeze.sqlite3") as connection, SqliteTransaction(connection):
            connection.run(
                "CREATE TABLE freeze_records(strategy TEXT,trade_date TEXT,version TEXT,payload_hash TEXT,committed_at TEXT,commit_kind TEXT,status TEXT,relative_path TEXT,payload_sha256 TEXT)"
            )
            connection.run(
                "CREATE TABLE freeze_checkpoints(strategy TEXT,trade_date TEXT,version TEXT,freeze_version TEXT,boundary_at TEXT,observed_at TEXT,relative_path TEXT,payload_sha256 TEXT,consumed_at TEXT)"
            )
            for strategy in (Strategy.TOMORROW, Strategy.D25):
                item = record(strategy)
                payload = CommittedRecordBytes(item)
                relative = strategy.value + ".json"
                (root / relative).write_bytes(payload)
                expected[profile + "/" + strategy.value] = payload
                connection.run(
                    "INSERT INTO freeze_records VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        strategy.value,
                        item.trade_date.isoformat(),
                        item.version,
                        item.payload_hash,
                        item.committed_at.isoformat(),
                        item.commit_kind,
                        "committed",
                        relative,
                        hashlib.sha256(payload).hexdigest(),
                    ),
                )
    return expected


def _fingerprint(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_candidate_keeps_old_formal_evidence_isolated_and_requests_rebuild(tmp_path):
    source = _source(tmp_path)
    _legacy_freeze(source)
    before = _fingerprint(source)
    target = tmp_path / "candidate"
    manifest = migrate(source, target, default_profile="v2", sqlite_policy=SqlitePolicyFixture())
    assert inspect_layout(target, sqlite_policy=SqlitePolicyFixture()) == manifest
    assert _fingerprint(source) == before
    assert not (target / "recommendation").exists()
    assert not (target / "research").exists()
    assert {"freeze_records", "publication_sequence", "outcomes", "freeze_traces", "research_experiments"} <= set(
        manifest.regenerate
    )


def test_uncheckpointed_source_never_publishes_or_changes_source(tmp_path):
    source = _source(tmp_path)
    _legacy_freeze(source)
    root = source / "data/freeze/v2"
    (root / "freeze.sqlite3-wal").write_bytes(b"uncheckpointed")
    before = _fingerprint(source)
    target = tmp_path / "candidate"
    with pytest.raises((ValueError, MigrationError)):
        migrate(source, target, default_profile="v2", sqlite_policy=SqlitePolicyFixture())
    assert not target.exists()
    assert _fingerprint(source) == before


def test_old_budget_and_sequences_are_not_imported(tmp_path):
    source = _source(tmp_path)
    runtime = source / ".runtime/trader"
    for index, root in enumerate((runtime, runtime / "profiles/v2")):
        root.mkdir(parents=True)
        with SqliteSession(root / "deepseek-budget.sqlite3") as connection, SqliteTransaction(connection):
            connection.run("CREATE TABLE calls(identity TEXT PRIMARY KEY,used INTEGER)")
            connection.run("INSERT INTO calls VALUES(?,?)", (str(index), 84))
        sequence = PublicationSequence(root / "publication-sequence.sqlite3", block_size=10, sqlite_policy=SqlitePolicyFixture())
        sequence.initialize()
        sequence.prepare(minimum=100 * (index + 1))
        sequence.allocate(minimum=100 * (index + 1))
    target = tmp_path / "candidate"
    migrate(source, target, default_profile="v2", sqlite_policy=SqlitePolicyFixture())
    assert not (target / "recommendation/shared/deepseek_budget.sqlite3").exists()
    assert not (target / "recommendation/v2").exists()


def test_conflicting_budget_identity_stops_instead_of_resetting(tmp_path):
    first, second, target = (tmp_path / name for name in ("first.sqlite3", "second.sqlite3", "target.sqlite3"))
    for path, used in ((first, 1), (second, 2)):
        with SqliteSession(path) as connection, SqliteTransaction(connection):
            connection.run("CREATE TABLE calls(identity TEXT PRIMARY KEY,used INTEGER)")
            connection.run("INSERT INTO calls VALUES('same',?)", (used,))
    MergeDatabase(first, target, sqlite_policy=SqlitePolicyFixture())
    before = target.read_bytes()
    with pytest.raises(ValueError, match="identity_conflict"):
        MergeDatabase(second, target, sqlite_policy=SqlitePolicyFixture())
    assert target.read_bytes() == before


def test_existing_target_and_repository_paths_are_rejected(tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "candidate"
    target.mkdir()
    (target / "sentinel").write_bytes(b"retained")
    with pytest.raises(MigrationError):
        migrate(source, target, default_profile="v2", sqlite_policy=SqlitePolicyFixture())
    assert (target / "sentinel").read_bytes() == b"retained"
    with pytest.raises(MigrationError, match="outside_repository"):
        migrate(Path(__file__).resolve().parents[3], tmp_path / "other", default_profile="v2", sqlite_policy=SqlitePolicyFixture())


def test_legacy_company_exclusions_are_not_imported(tmp_path):
    source = _source(tmp_path)
    reference = source / "data/reference"
    snapshot = reference / "snapshots/fixture"
    snapshot.mkdir(parents=True)
    facts = tuple(sorted((_fact(), ManualBlacklistFact("600002", NOW, "a" * 64))))
    with SqliteSession(snapshot / "facts.sqlite3") as connection, SqliteTransaction(connection):
        connection.run(
            "CREATE TABLE issuer_eligibility_facts(code,reason,effective_at,evidence_id,source,evidence_hash,content_hash)"
        )
        connection.run_many(
            "INSERT INTO issuer_eligibility_facts VALUES(?,?,?,?,?,?,?)",
            (
                (
                    fact.code,
                    fact.reason.value,
                    fact.effective_at.isoformat(),
                    fact.evidence_id,
                    fact.source,
                    fact.evidence_hash,
                    IssuerEligibilityFactHash(fact),
                )
                for fact in facts
            ),
        )
    digest = hashlib.sha256("\n".join(IssuerEligibilityFactHash(fact) for fact in facts).encode()).hexdigest()
    (reference / "active-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "issuer_eligibility_snapshot",
                "snapshot_id": "fixture",
                "fact_count": 2,
                "manifest_hash": digest,
                "refresh_state": "failed",
                "last_source": "refresh_failure",
                "last_refresh_at": NOW.isoformat(),
                "next_refresh_at": (NOW + timedelta(hours=1)).isoformat(),
                "refresh_failure_count": 3,
            }
        )
    )
    target = tmp_path / "candidate"
    migrate(source, target, default_profile="v2", sqlite_policy=SqlitePolicyFixture())
    assert not (target / "company/eligibility.sqlite3").exists()
    assert not (target / "company/company_facts.sqlite3").exists()
