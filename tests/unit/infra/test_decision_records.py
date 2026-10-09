from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.domain.test_decision_identity import NOW, decision, pipeline, stage_snapshots
from trader.http_api.response.decision_projection import serialize_decision_view, serialize_event
from trader.recommendation.application.pipeline.freeze_publish.decision_events import build_decision_committed
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.application.ports.decision_records import (
    DecisionCheckpoint,
    DecisionRecordConflictError,
    DecisionRecordUnavailableError,
)
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence import decision_records as decision_records_module
from trader.recommendation.infra.persistence.decision_records import SQLiteDecisionRecords


def record(strategy: Strategy = Strategy.TOMORROW, *, sequence: int = 1) -> CommittedDecisionRecord:
    return CommittedDecisionRecord(decision(strategy, sequence=sequence), NOW, "scheduled")


def test_fourteen_stage_audit_restarts_and_has_identical_get_sse_projections(tmp_path: Path) -> None:
    class Clock:
        def now(self) -> datetime:
            return NOW

    current = replace(decision(), pipeline=replace(pipeline(), stage_snapshots=stage_snapshots()))
    expected = CommittedDecisionRecord(current, NOW, "scheduled")
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    records.commit(expected)
    restarted = SQLiteDecisionRecords(tmp_path)
    restored = restarted.load(Strategy.TOMORROW, current.trade_date)
    assert restored == expected
    queries = UnifiedDecisionQueries(UnifiedDecisionIndex(), UnifiedDecisionDraftIndex(), restarted, Clock())
    history = serialize_decision_view(queries.history(Strategy.TOMORROW, current.trade_date))
    event = serialize_event(UnifiedDecisionEventStream().publish_committed(build_decision_committed(restored.decision)))
    assert history["pipeline"] == event["pipeline"]
    assert len(history["pipeline"]["stage_snapshots"]) == 14


def test_formal_records_are_idempotent_and_isolated_by_strategy_and_date(tmp_path: Path) -> None:
    records = SQLiteDecisionRecords(tmp_path)
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
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    older_at = NOW - timedelta(days=1)
    fixture = decision()
    quote = fixture.items[0].quote
    assert quote is not None
    older_decision = replace(
        fixture,
        trade_date=older_at.date(),
        observed_at=older_at,
        items=(replace(fixture.items[0], quote=replace(quote, source_time=older_at)),),
    )
    older = CommittedDecisionRecord(older_decision, older_at, "scheduled")
    newest = record()
    records.commit(older)
    records.commit(newest)

    assert records.list_dates(Strategy.TOMORROW, limit=1) == (newest.trade_date,)
    assert records.list_dates(Strategy.TOMORROW, limit=2) == (newest.trade_date, older.trade_date)
    assert records.list_dates(Strategy.D25) == ()
    with pytest.raises(ValueError, match="between 1 and 366"):
        records.list_dates(Strategy.TOMORROW, limit=0)


def test_same_strategy_date_hash_conflict_is_rejected(tmp_path: Path) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    records.commit(record())

    with pytest.raises(DecisionRecordConflictError, match="already committed"):
        records.commit(record(sequence=2))


def test_same_record_is_idempotent_across_concurrent_record_instances(tmp_path: Path) -> None:
    first = SQLiteDecisionRecords(tmp_path)
    second = SQLiteDecisionRecords(tmp_path)
    first.initialize()
    expected = record()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda records: records.commit(expected), (first, second)))

    assert results == (None, None)
    assert first.load(Strategy.TOMORROW, expected.trade_date) == expected


def test_staged_half_commit_recovers_the_same_payload(tmp_path: Path) -> None:
    def fail_after_stage(point: str) -> None:
        if point == "manifest_staged":
            raise RuntimeError("injected")

    expected = record()
    failing = SQLiteDecisionRecords(tmp_path, fault_injector=fail_after_stage)
    failing.initialize()
    with pytest.raises(RuntimeError, match="injected"):
        failing.commit(expected)

    recovered = SQLiteDecisionRecords(tmp_path)
    summary = recovered.recover()

    assert summary.recovered == 1
    assert recovered.load(Strategy.TOMORROW, expected.trade_date) == expected


def test_committed_load_hashes_file_bytes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    expected = record()
    records.commit(expected)
    original = decision_records_module._sha256
    calls = 0

    def count_hash(payload: bytes) -> str:
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(decision_records_module, "_sha256", count_hash)

    assert records.load(Strategy.TOMORROW, expected.trade_date) == expected
    assert calls == 1


def test_committed_recovery_hashes_each_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    records.commit(record())
    original = decision_records_module._sha256
    calls = 0

    def count_hash(payload: bytes) -> str:
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(decision_records_module, "_sha256", count_hash)

    summary = records.recover()

    assert summary.quarantined == 0
    assert calls == 1


def test_corrupted_committed_record_is_quarantined_and_fails_closed(tmp_path: Path) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    expected = record()
    records.commit(expected)
    payload = next((tmp_path / "decisions" / "records").rglob("*.json"))
    payload.write_bytes(b"corrupt")

    summary = records.recover()

    assert summary.quarantined == 1
    with pytest.raises(DecisionRecordUnavailableError, match="quarantined"):
        records.load(Strategy.TOMORROW, expected.trade_date)
    assert next((tmp_path / "decisions" / "quarantine").rglob("*.json")).is_file()


def test_invalid_manifest_path_is_quarantined_without_accessing_outside_root(tmp_path: Path) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    expected = record()
    records.commit(expected)
    database = tmp_path / "decisions" / "decisions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE decision_records SET relative_path = '../outside.json' WHERE strategy = ?",
            (Strategy.TOMORROW.value,),
        )

    summary = records.recover()

    assert summary.quarantined == 1
    with pytest.raises(DecisionRecordUnavailableError, match="quarantined"):
        records.load(Strategy.TOMORROW, expected.trade_date)


def test_checkpoint_round_trip_is_verified_and_consumed(tmp_path: Path) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    boundary = NOW.replace(hour=14, minute=50)
    checkpoint = DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)

    records.save_checkpoint(checkpoint)
    records.save_checkpoint(checkpoint)

    assert records.load_checkpoint(Strategy.TOMORROW, NOW.date()) == checkpoint
    records.consume_checkpoint(checkpoint, consumed_at=boundary)
    assert records.load_checkpoint(Strategy.TOMORROW, NOW.date()) is None


def test_concurrent_checkpoint_writers_retain_the_newest_observation(tmp_path: Path) -> None:
    first = SQLiteDecisionRecords(tmp_path)
    second = SQLiteDecisionRecords(tmp_path)
    first.initialize()
    boundary = NOW.replace(hour=14, minute=50)
    older = DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)
    newer = DecisionCheckpoint(
        replace(decision(sequence=3), observed_at=boundary - timedelta(seconds=10)),
        boundary,
    )

    def save(item) -> str:
        records, checkpoint = item
        try:
            records.save_checkpoint(checkpoint)
        except DecisionRecordConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(save, ((first, older), (second, newer))))

    assert "saved" in outcomes
    assert first.load_checkpoint(Strategy.TOMORROW, NOW.date()) == newer


def test_corrupted_checkpoint_is_quarantined_and_never_restored(tmp_path: Path) -> None:
    records = SQLiteDecisionRecords(tmp_path)
    records.initialize()
    boundary = NOW.replace(hour=14, minute=50)
    checkpoint = DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)
    records.save_checkpoint(checkpoint)
    payload = next((tmp_path / "decisions" / "checkpoints").rglob("*.json"))
    payload.write_bytes(b"corrupt")

    summary = records.recover()

    assert summary.quarantined == 1
    assert records.load_checkpoint(Strategy.TOMORROW, NOW.date()) is None
    assert next((tmp_path / "decisions" / "quarantine" / "checkpoint_invalid").rglob("*.json")).is_file()
