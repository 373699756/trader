from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.migrate_runtime_data import MigrationError, inspect_layout, migrate
from scripts.runtime_migration import ImportTraces, MergeDatabase
from tests.unit.infra.test_decision_records import NOW, decision, record
from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.recommendation.application.ports.decision_records import DecisionCheckpoint, DecisionRecordConflictError
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord
from trader.recommendation.domain.market.eligibility import IssuerEligibilityFactHash, ManualBlacklistFact
from trader.recommendation.infra.persistence.issuer_eligibility import SQLiteIssuerEligibilityIndex
from tests.unit.infra.test_issuer_eligibility_index import _fact
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.decision_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords
from trader.recommendation.infra.persistence.publication_sequence import PublicationSequence
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.training.infra.research.trace_codec import PersistedPayloadBytes
from trader.training.infra.research.trace_storage import InitializeDatabase


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "data").mkdir(parents=True)
    return source


def _legacy_freeze(source: Path) -> dict[str, bytes]:
    expected = {}
    for profile in ("v2", "v3"):
        root = source / "data/freeze" / profile
        root.mkdir(parents=True)
        with closing(OpenSqliteConnection(root / "freeze.sqlite3")) as connection, connection:
            connection.run(
                "CREATE TABLE decision_records(strategy TEXT,trade_date TEXT,version TEXT,payload_hash TEXT,committed_at TEXT,commit_kind TEXT,status TEXT,relative_path TEXT,payload_sha256 TEXT)"
            )
            connection.run(
                "CREATE TABLE decision_checkpoints(strategy TEXT,trade_date TEXT,version TEXT,decision_version TEXT,boundary_at TEXT,observed_at TEXT,relative_path TEXT,payload_sha256 TEXT,consumed_at TEXT)"
            )
            for strategy in (Strategy.TOMORROW, Strategy.D25):
                item = record(strategy)
                payload = CommittedRecordBytes(item)
                relative = strategy.value + ".json"
                (root / relative).write_bytes(payload)
                expected[profile + "/" + strategy.value] = payload
                connection.run(
                    "INSERT INTO decision_records VALUES(?,?,?,?,?,?,?,?,?)",
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


def test_candidate_preserves_exact_formal_bytes_and_first_wins(tmp_path):
    source = _source(tmp_path)
    expected = _legacy_freeze(source)
    before = _fingerprint(source)
    target = tmp_path / "candidate"
    manifest = migrate(source, target, default_profile="v2")
    assert inspect_layout(target) == manifest
    assert _fingerprint(source) == before
    for profile in ("v2", "v3"):
        path = target / "recommendation" / profile / "decisions.sqlite3"
        records = SQLiteDecisionRecords(path)
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            assert records.load(strategy, NOW.date()) == record(strategy)
            with closing(OpenSqliteConnection(path)) as connection:
                payload = connection.run(
                    "SELECT payload FROM decision_records WHERE strategy=?", (strategy.value,)
                ).require_one()[0]
            assert payload == expected[profile + "/" + strategy.value]
        with pytest.raises(DecisionRecordConflictError):
            records.commit(record(sequence=3))
    assert not tuple(target.rglob("tomorrow.json"))


@pytest.mark.parametrize("consumed", (False, True))
def test_checkpoint_bytes_and_consumption_survive_migration(tmp_path, consumed):
    source = _source(tmp_path)
    _legacy_freeze(source)
    root = source / "data/freeze/v2"
    boundary = NOW.replace(hour=14, minute=50)
    checkpoint = DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)
    payload = CommittedRecordBytes(CommittedDecisionRecord(checkpoint.decision, boundary, "checkpoint_recovery"))
    (root / "checkpoint.json").write_bytes(payload)
    with closing(OpenSqliteConnection(root / "freeze.sqlite3")) as connection, connection:
        connection.run(
            "INSERT INTO decision_checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "tomorrow",
                NOW.date().isoformat(),
                checkpoint.version,
                checkpoint.decision.version,
                boundary.isoformat(),
                checkpoint.decision.observed_at.isoformat(),
                "checkpoint.json",
                hashlib.sha256(payload).hexdigest(),
                boundary.isoformat() if consumed else None,
            ),
        )
    target = tmp_path / "candidate"
    migrate(source, target, default_profile="v2")
    database = target / "recommendation/v2/decisions.sqlite3"
    assert SQLiteDecisionRecords(database).load_checkpoint(Strategy.TOMORROW, NOW.date()) == (
        None if consumed else checkpoint
    )
    with closing(OpenSqliteConnection(database)) as connection:
        assert connection.run("SELECT payload FROM decision_checkpoints").require_one()[0] == payload


@pytest.mark.parametrize("damage", ("corrupt", "orphan", "wal", "staged"))
def test_unverified_source_never_publishes_or_changes_source(tmp_path, damage):
    source = _source(tmp_path)
    _legacy_freeze(source)
    root = source / "data/freeze/v2"
    if damage == "corrupt":
        (root / "tomorrow.json").write_bytes(b"corrupt")
    elif damage == "orphan":
        (root / "orphan.json").write_bytes(b"unindexed")
    elif damage == "wal":
        (root / "freeze.sqlite3-wal").write_bytes(b"uncheckpointed")
    else:
        with closing(OpenSqliteConnection(root / "freeze.sqlite3")) as connection, connection:
            connection.run("UPDATE decision_records SET status='staged'")
    before = _fingerprint(source)
    target = tmp_path / "candidate"
    with pytest.raises((ValueError, MigrationError)):
        migrate(source, target, default_profile="v2")
    assert not target.exists()
    assert _fingerprint(source) == before


def test_budget_rows_and_sequence_reservations_survive_profile_merge(tmp_path):
    source = _source(tmp_path)
    runtime = source / ".runtime/trader"
    for index, root in enumerate((runtime, runtime / "profiles/v2")):
        root.mkdir(parents=True)
        with closing(OpenSqliteConnection(root / "deepseek-budget.sqlite3")) as connection, connection:
            connection.run("CREATE TABLE calls(identity TEXT PRIMARY KEY,used INTEGER)")
            connection.run("INSERT INTO calls VALUES(?,?)", (str(index), 84))
        sequence = PublicationSequence(root / "publication-sequence.sqlite3", block_size=10)
        sequence.initialize()
        sequence.allocate(minimum=100 * (index + 1))
    target = tmp_path / "candidate"
    migrate(source, target, default_profile="v2")
    with closing(OpenSqliteConnection(target / "recommendation/shared/deepseek_budget.sqlite3")) as connection:
        assert connection.run("SELECT SUM(used) FROM calls").require_one()[0] == 168
    sequence = PublicationSequence(target / "recommendation/v2/snapshots.sqlite3", block_size=10)
    sequence.initialize()
    assert sequence.allocate() > 200


def test_conflicting_budget_identity_stops_instead_of_resetting(tmp_path):
    first, second, target = (tmp_path / name for name in ("first.sqlite3", "second.sqlite3", "target.sqlite3"))
    for path, used in ((first, 1), (second, 2)):
        with closing(OpenSqliteConnection(path)) as connection, connection:
            connection.run("CREATE TABLE calls(identity TEXT PRIMARY KEY,used INTEGER)")
            connection.run("INSERT INTO calls VALUES('same',?)", (used,))
    MergeDatabase(first, target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="identity_conflict"):
        MergeDatabase(second, target)
    assert target.read_bytes() == before


def test_real_budget_reservations_keep_the_global_168_limit_after_merge(tmp_path):
    source = _source(tmp_path)
    runtime = source / ".runtime/trader"

    def ledger(path):
        result = DeepSeekBudgetLedger(
            path,
            daily_hard_limit=168,
            strategy_limits={"tomorrow": 168, "d25": 0, "shared_preheat": 0, "emergency": 0},
            stage_targets={"tomorrow_morning": 0},
            stage_limits={"tomorrow_morning": 168},
        )
        result.initialize()
        return result

    for root in (runtime, runtime / "profiles/v3"):
        budget = ledger(root / "deepseek-budget.sqlite3")
        for _ in range(84):
            assert budget.reserve(Strategy.TOMORROW, phase="morning_main", requested_at=NOW).allowed
    target = tmp_path / "candidate"
    migrate(source, target, default_profile="v2")
    merged = ledger(target / "recommendation/shared/deepseek_budget.sqlite3")
    assert not merged.reserve(Strategy.TOMORROW, phase="morning_main", requested_at=NOW).allowed


def test_compressed_quarantine_bytes_survive_partitioning(tmp_path):
    source = tmp_path / "traces.sqlite3"
    InitializeDatabase(source, set())
    payload = PersistedPayloadBytes(json.dumps({"trade_date": "2026-10-16", "padding": "a" * 2000}).encode())
    with closing(OpenSqliteConnection(source)) as connection, connection:
        connection.run(
            "INSERT INTO committed_event_quarantine VALUES(?,?,?,?)", ("decision", "fixture", "hash", payload)
        )
    target = tmp_path / "traces"
    ImportTraces(source, target)
    with closing(OpenSqliteConnection(target / "2026-10-16.sqlite3")) as connection:
        assert connection.run("SELECT payload FROM committed_event_quarantine").require_one()[0] == payload


def test_existing_target_and_repository_paths_are_rejected(tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "candidate"
    target.mkdir()
    (target / "sentinel").write_bytes(b"retained")
    with pytest.raises(MigrationError):
        migrate(source, target, default_profile="v2")
    assert (target / "sentinel").read_bytes() == b"retained"
    with pytest.raises(MigrationError, match="outside_repository"):
        migrate(Path(__file__).resolve().parents[3], tmp_path / "other", default_profile="v2")


def test_eligibility_facts_blacklist_and_refresh_identity_survive_migration(tmp_path):
    source = _source(tmp_path)
    reference = source / "data/reference"
    snapshot = reference / "snapshots/fixture"
    snapshot.mkdir(parents=True)
    facts = tuple(sorted((_fact(), ManualBlacklistFact("600002", NOW, "a" * 64))))
    with closing(OpenSqliteConnection(snapshot / "facts.sqlite3")) as connection, connection:
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
    migrate(source, target, default_profile="v2")
    index = SQLiteIssuerEligibilityIndex(target / "company/eligibility.sqlite3", read_only=True)
    assert index.facts() == facts
    assert index.filter_codes(("600001", "600002", "600003"), NOW + timedelta(days=100)) == ("600003",)
    status = index.status()
    assert status.integrity_ok and status.manifest_hash == digest
    assert status.refresh_state == "failed" and status.refresh_failure_count == 3
    assert status.last_refresh_at == NOW and status.next_refresh_at == NOW + timedelta(hours=1)
