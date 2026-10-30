from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.domain.test_freeze_identity import NOW, freeze, pipeline, stage_snapshots
from trader.http_api.response.freeze_projection import SerializeEvent, SerializeFreezeView
from trader.infra.cache import BoundedLruCache
from trader.infra.cache_contracts import CacheDatasetPolicy, CacheGroupPolicy, CachePolicy
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import BuildFreezeCommitted
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.freeze_records import (
    FreezeRecordConflictError,
    FreezeRecordUnavailableError,
)
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.score_ledger import ScoreAdjustmentItem, ScoreLedgerPayload
from trader.recommendation.domain.scoring.company import (
    BuildScoreLedger,
    CompanyAdjustments,
    ScoreLedgerInput,
)
from tests.helpers.configuration import CompanyScoringPolicyFixture as CompanyScoringPolicy
from trader.recommendation.infra.persistence import freeze_records as freeze_records_module
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords


def record(strategy: Strategy = Strategy.TOMORROW, *, sequence: int = 1) -> CommittedFreezeRecord:
    return CommittedFreezeRecord(freeze(strategy, sequence=sequence), NOW, "scheduled")


def test_eleven_stage_audit_restarts_and_has_identical_get_sse_projections(tmp_path: Path) -> None:
    class Clock:
        def now(self) -> datetime:
            return NOW

    current = replace(freeze(), pipeline=replace(pipeline(), stage_snapshots=stage_snapshots()))
    expected = CommittedFreezeRecord(current, NOW, "scheduled")
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    records.commit(expected)
    restarted = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    restored = restarted.load(Strategy.TOMORROW, current.trade_date)
    assert restored.version == expected.version and restored.payload_hash == expected.payload_hash
    assert CommittedRecordBytes(restored) == CommittedRecordBytes(expected)
    queries = UnifiedFreezeQueries(UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), restarted, Clock())
    history = SerializeFreezeView(queries.history(Strategy.TOMORROW, current.trade_date))
    event = SerializeEvent(UnifiedFreezeEventStream().publish_committed(BuildFreezeCommitted(restored.freeze)))
    assert history["pipeline"] == event["pipeline"]
    assert len(history["pipeline"]["stage_snapshots"]) == 11


def test_score_ledger_survives_freeze_reload_and_get_sse_without_changing_old_records(tmp_path):
    class Clock:
        def now(self):
            return NOW

    old = record()
    old_bytes = CommittedRecordBytes(old)
    ledger = BuildScoreLedger(
        ScoreLedgerInput(
            84,
            CompanyAdjustments(
                (ScoreAdjustmentItem("financial", "financial_bonus", "财务奖励", 0, 2, 0, "eligible"),),
                (),
                0,
                NOW.date(),
            ),
            (),
        ),
        strategy="tomorrow",
        policy=CompanyScoringPolicy(enabled=True),
        risk_cap=25,
    )
    new = CommittedFreezeRecord(
        replace(
            old.freeze,
            items=(replace(old.freeze.items[0], local_score=86.0, final_score=86.0, score_ledger=ledger),),
        ),
        NOW,
        "scheduled",
    )
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3")
    records.initialize()
    records.commit(new)
    restored = records.load(Strategy.TOMORROW, NOW.date())
    queries = UnifiedFreezeQueries(UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), records, Clock())
    get = SerializeFreezeView(queries.history(Strategy.TOMORROW, NOW.date()))
    event = SerializeEvent(UnifiedFreezeEventStream().publish_committed(BuildFreezeCommitted(restored.freeze)))
    assert get["items"][0]["scores"]["ledger"] == event["upserts"][0]["scores"]["ledger"] == ScoreLedgerPayload(ledger)
    assert CommittedRecordBytes(restored) == CommittedRecordBytes(new)
    assert CommittedRecordBytes(old) == old_bytes and b'"score_ledger"' not in old_bytes


def test_formal_records_are_idempotent_and_isolated_by_strategy_and_date(tmp_path: Path) -> None:
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    tomorrow = record()
    today = record(Strategy.TOMORROW)

    records.commit(tomorrow)
    records.commit(tomorrow)
    records.commit(today)

    assert records.load(Strategy.TOMORROW, tomorrow.trade_date) == tomorrow
    assert records.load(Strategy.TOMORROW, today.trade_date) == today
    assert records.load(Strategy.D25, tomorrow.trade_date) is None


def test_formal_record_dates_are_bounded_and_newest_first(tmp_path: Path) -> None:
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    older_at = NOW - timedelta(days=1)
    fixture = freeze()
    quote = fixture.items[0].quote
    assert quote is not None
    older_freeze = replace(
        fixture,
        trade_date=older_at.date(),
        observed_at=older_at,
        items=(replace(fixture.items[0], quote=replace(quote, source_time=older_at)),),
    )
    older = CommittedFreezeRecord(older_freeze, older_at, "scheduled")
    newest = record()
    records.commit(older)
    records.commit(newest)

    assert records.list_dates(Strategy.TOMORROW, limit=1) == (newest.trade_date,)
    assert records.list_dates(Strategy.TOMORROW, limit=2) == (newest.trade_date, older.trade_date)
    assert records.list_dates(Strategy.D25) == ()
    with pytest.raises(ValueError, match="between 1 and 366"):
        records.list_dates(Strategy.TOMORROW, limit=0)


def test_same_strategy_date_hash_conflict_is_rejected(tmp_path: Path) -> None:
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    records.commit(record())

    with pytest.raises(FreezeRecordConflictError, match="already committed"):
        records.commit(record(sequence=2))


def test_formal_records_and_payloads_share_one_database(tmp_path):
    path = tmp_path / "freeze.sqlite3"
    records = SQLiteFreezeRecords(path)
    records.initialize()
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        records.commit(record(strategy))
    assert {item.name for item in tmp_path.iterdir() if not item.name.endswith(("-wal", "-shm"))} == {"freeze.sqlite3"}
    assert not tuple(tmp_path.rglob("*.json"))
    assert SQLiteFreezeRecords(path).load(Strategy.D25, NOW.date()) == record(Strategy.D25)
    with SqliteSession(path) as connection:
        tables = {row[0] for row in connection.run("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"schema_contract", "freeze_record"}
        assert connection.run("PRAGMA user_version").require_one()[0] == 2
        persisted = connection.run("SELECT typeof(trade_date), typeof(committed_at), length(freeze_hash), length(record_hash) FROM freeze_record").require_one()
        assert tuple(persisted) == ("integer", "integer", 32, 32)


def test_unversioned_legacy_database_is_rejected(tmp_path):
    path = tmp_path / "freeze.sqlite3"
    with SqliteSession(path) as connection:
        connection.run("CREATE TABLE legacy(value TEXT)")
    with pytest.raises(FreezeRecordUnavailableError):
        SQLiteFreezeRecords(path).initialize()


@pytest.mark.parametrize("cached", (False, True))
def test_failed_transaction_does_not_leave_partial_payload_or_index(tmp_path, cached):
    def fail(point):
        if point == "before_commit":
            raise KeyboardInterrupt

    path = tmp_path / "freeze.sqlite3"
    records = SQLiteFreezeRecords(path, fault_injector=fail, cache=_read_cache() if cached else None)
    records.initialize()
    with pytest.raises(KeyboardInterrupt):
        records.commit(record())
    assert records.load(Strategy.TOMORROW, NOW.date()) is None
    assert records.list_dates(Strategy.TOMORROW) == ()
    SQLiteFreezeRecords(path).commit(record())
    assert records.load(Strategy.TOMORROW, NOW.date()) == record()


def test_same_record_is_idempotent_across_concurrent_record_instances(tmp_path: Path) -> None:
    first = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    second = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    first.initialize()
    expected = record()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda records: records.commit(expected), (first, second)))

    assert results == (None, None)
    assert first.load(Strategy.TOMORROW, expected.trade_date) == expected


def test_staged_half_commit_recovers_the_same_payload(tmp_path: Path) -> None:
    def fail_after_stage(point: str) -> None:
        if point == "after_commit":
            raise RuntimeError("injected")

    expected = record()
    failing = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3", fault_injector=fail_after_stage)
    failing.initialize()
    with pytest.raises(RuntimeError, match="injected"):
        failing.commit(expected)

    recovered = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    summary = recovered.recover()

    assert summary.recovered == 0
    assert recovered.load(Strategy.TOMORROW, expected.trade_date) == expected


def test_committed_load_hashes_file_bytes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    expected = record()
    records.commit(expected)
    original = freeze_records_module._bounded_digest
    calls = 0

    def count_hash(payload: bytes) -> str:
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(freeze_records_module, "_bounded_digest", count_hash)

    assert records.load(Strategy.TOMORROW, expected.trade_date) == expected
    assert calls == 1


def test_committed_recovery_hashes_each_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    records.commit(record())
    original = freeze_records_module._bounded_digest
    calls = 0

    def count_hash(payload: bytes) -> str:
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(freeze_records_module, "_bounded_digest", count_hash)

    summary = records.recover()

    assert summary.quarantined == 0
    assert calls == 1


def test_corrupted_committed_record_is_quarantined_and_fails_closed(tmp_path: Path) -> None:
    records = SQLiteFreezeRecords((tmp_path) / "freeze.sqlite3")
    records.initialize()
    expected = record()
    records.commit(expected)
    with SqliteSession(tmp_path / "freeze.sqlite3") as connection, SqliteTransaction(connection):
        connection.run("UPDATE freeze_record SET record_bytes=?", (b"corrupt",))

    summary = records.recover()

    assert summary.quarantined == 1
    with pytest.raises(FreezeRecordUnavailableError, match="quarantined"):
        records.load(Strategy.TOMORROW, expected.trade_date)
    assert not tuple(tmp_path.rglob("*.json"))


def test_invalid_index_identity_is_quarantined_without_rewriting_payload(tmp_path):
    path = tmp_path / "freeze.sqlite3"
    records = SQLiteFreezeRecords(path)
    records.initialize()
    records.commit(record())
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        before = connection.run("SELECT record_bytes FROM freeze_record").require_one()[0]
        connection.run("UPDATE freeze_record SET freeze_id='invalid'")
    assert records.recover().quarantined == 1
    with SqliteSession(path) as connection:
        assert connection.run("SELECT record_bytes FROM freeze_record").require_one()[0] == before
    with pytest.raises(FreezeRecordUnavailableError):
        records.load(Strategy.TOMORROW, NOW.date())


def _read_cache(*, capacity: int = 72, group_bytes: int = 1_000_000) -> BoundedLruCache[object]:
    policy = CachePolicy(
        datasets={
            dataset: CacheDatasetPolicy(
                86400,
                86400,
                None,
                None,
                60,
                capacity,
                "projection",
                False,
            )
            for dataset in ("published_recommendation_view", "published_date_index")
        },
        groups={"projection": CacheGroupPolicy(group_bytes)},
        total_bytes=group_bytes,
        runtime_reserve_bytes=1024,
        pool_total_bytes=group_bytes + 1024,
        estimator="canonical_json_utf8",
    )
    return BoundedLruCache(policy, wall_clock=lambda: NOW)


def test_startup_skips_integrity_scan_and_historical_payload_decoding(tmp_path, monkeypatch):
    path = tmp_path / "freeze.sqlite3"
    records = SQLiteFreezeRecords(path)
    records.initialize()
    records.commit(record())
    records.commit(record(Strategy.D25))
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE freeze_record SET record_bytes=? WHERE strategy='d25'", (b"corrupt",))

    def unexpected(*args, **kwargs):
        pytest.fail("normal startup must not scan or decode historical records")

    restarted = SQLiteFreezeRecords(path, cache=_read_cache(), wall_clock=lambda: NOW)
    with monkeypatch.context() as startup:
        startup.setattr(freeze_records_module, "CheckSqliteIntegrity", unexpected)
        startup.setattr(restarted, "_load_record", unexpected)
        restarted.initialize()
    assert restarted.load(Strategy.TOMORROW, NOW.date()) == record()
    with pytest.raises(FreezeRecordUnavailableError, match="hash mismatch"):
        restarted.load(Strategy.D25, NOW.date())
    with SqliteSession(path) as connection:
        assert connection.run("SELECT status FROM freeze_record WHERE strategy='d25'").require_one()[0] == "committed"


def test_repeated_record_and_date_queries_reuse_memory_without_database_access(tmp_path, monkeypatch):
    cache = _read_cache()
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", cache=cache, wall_clock=lambda: NOW)
    records.initialize()
    expected = record()
    records.commit(expected)
    assert records.load(Strategy.TOMORROW, NOW.date()) == expected
    assert records.list_dates(Strategy.TOMORROW, limit=1) == (NOW.date(),)

    def unexpected(*args, **kwargs):
        pytest.fail("repeated freeze queries must not open a database or decode a payload")

    monkeypatch.setattr(freeze_records_module, "SqliteSession", unexpected)
    monkeypatch.setattr(freeze_records_module, "CommittedRecordFromBytes", unexpected)
    for _ in range(5):
        assert records.load(Strategy.TOMORROW, NOW.date()) == expected
        assert records.list_dates(Strategy.TOMORROW, limit=366) == (NOW.date(),)
    assert sum(item.estimated_bytes for sources in cache.status().datasets.values() for item in sources.values()) > 0


def test_commit_updates_cached_dates_without_allowing_a_conflicting_winner(tmp_path):
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", cache=_read_cache(), wall_clock=lambda: NOW)
    records.initialize()
    assert records.list_dates(Strategy.TOMORROW) == ()
    assert records.load(Strategy.TOMORROW, NOW.date()) is None
    records.commit(record())
    assert records.list_dates(Strategy.TOMORROW) == (NOW.date(),)
    assert records.load(Strategy.TOMORROW, NOW.date()) == record()
    with pytest.raises(FreezeRecordConflictError):
        records.commit(record(sequence=2))
    assert records.load(Strategy.TOMORROW, NOW.date()) == record()


@pytest.mark.parametrize("group_bytes", (100, 1_000_000))
def test_cache_capacity_and_eviction_preserve_verified_reads(tmp_path, group_bytes):
    cache = _read_cache(capacity=1, group_bytes=group_bytes)
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", cache=cache, wall_clock=lambda: NOW)
    records.initialize()
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        records.commit(record(strategy))
    for strategy in (Strategy.TOMORROW, Strategy.D25, Strategy.TOMORROW):
        assert records.load(strategy, NOW.date()) == record(strategy)
    assert sum(item.estimated_bytes for sources in cache.status().datasets.values() for item in sources.values()) <= group_bytes


def test_shared_cache_isolates_profiles_and_recovery_invalidates_only_its_database(tmp_path):
    cache = _read_cache()
    first = SQLiteFreezeRecords(tmp_path / "v1.sqlite3", cache=cache, wall_clock=lambda: NOW)
    second = SQLiteFreezeRecords(tmp_path / "v2.sqlite3", cache=cache, wall_clock=lambda: NOW)
    for records, expected in ((first, record()), (second, record(sequence=2))):
        records.initialize()
        records.commit(expected)
        assert records.load(Strategy.TOMORROW, NOW.date()) == expected
        assert records.list_dates(Strategy.TOMORROW) == (NOW.date(),)
    with SqliteSession(tmp_path / "v1.sqlite3") as connection, SqliteTransaction(connection):
        connection.run("UPDATE freeze_record SET record_bytes=?", (b"corrupt",))
    assert first.recover().quarantined == 1
    assert first.list_dates(Strategy.TOMORROW) == ()
    with pytest.raises(FreezeRecordUnavailableError, match="quarantined"):
        first.load(Strategy.TOMORROW, NOW.date())
    assert second.load(Strategy.TOMORROW, NOW.date()) == record(sequence=2)
    assert second.list_dates(Strategy.TOMORROW) == (NOW.date(),)
