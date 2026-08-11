from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.ports.decision_records import (
    DecisionRecordConflictError,
    DecisionRecordUnavailableError,
    V2DecisionCheckpoint,
)
from trader.domain.recommendation.decision_identity import CommittedDecisionRecord
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository


def record(strategy: Strategy = Strategy.TOMORROW, *, sequence: int = 1) -> CommittedDecisionRecord:
    return CommittedDecisionRecord(decision(strategy, sequence=sequence), NOW, "scheduled")


def test_formal_records_are_idempotent_and_isolated_by_strategy_and_date(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    tomorrow = record()
    today = record(Strategy.TODAY)

    repository.commit(tomorrow)
    repository.commit(tomorrow)
    repository.commit(today)

    assert repository.load(Strategy.TOMORROW, tomorrow.trade_date) == tomorrow
    assert repository.load(Strategy.TODAY, today.trade_date) == today
    assert repository.load(Strategy.D25, tomorrow.trade_date) is None


def test_formal_record_dates_are_bounded_and_newest_first(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    older_at = NOW - timedelta(days=1)
    older_decision = replace(decision(), trade_date=older_at.date(), observed_at=older_at)
    older = CommittedDecisionRecord(older_decision, older_at, "scheduled")
    newest = record()
    repository.commit(older)
    repository.commit(newest)

    assert repository.list_dates(Strategy.TOMORROW, limit=1) == (newest.trade_date,)
    assert repository.list_dates(Strategy.TOMORROW, limit=2) == (newest.trade_date, older.trade_date)
    assert repository.list_dates(Strategy.TODAY) == ()
    with pytest.raises(ValueError, match="between 1 and 366"):
        repository.list_dates(Strategy.TOMORROW, limit=0)


def test_same_strategy_date_hash_conflict_is_rejected(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    repository.commit(record())

    with pytest.raises(DecisionRecordConflictError, match="already committed"):
        repository.commit(record(sequence=2))


def test_same_record_is_idempotent_across_concurrent_repository_instances(tmp_path: Path) -> None:
    first = SQLiteDecisionRecordRepository(tmp_path)
    second = SQLiteDecisionRecordRepository(tmp_path)
    first.initialize()
    expected = record()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda repository: repository.commit(expected), (first, second)))

    assert results == (None, None)
    assert first.load(Strategy.TOMORROW, expected.trade_date) == expected


def test_staged_half_commit_recovers_the_same_payload(tmp_path: Path) -> None:
    def fail_after_stage(point: str) -> None:
        if point == "manifest_staged":
            raise RuntimeError("injected")

    expected = record()
    failing = SQLiteDecisionRecordRepository(tmp_path, fault_injector=fail_after_stage)
    failing.initialize()
    with pytest.raises(RuntimeError, match="injected"):
        failing.commit(expected)

    recovered = SQLiteDecisionRecordRepository(tmp_path)
    summary = recovered.recover()

    assert summary.recovered == 1
    assert recovered.load(Strategy.TOMORROW, expected.trade_date) == expected


def test_corrupted_committed_record_is_quarantined_and_fails_closed(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    expected = record()
    repository.commit(expected)
    payload = next((tmp_path / "v2-decisions" / "records").rglob("*.json"))
    payload.write_bytes(b"corrupt")

    summary = repository.recover()

    assert summary.quarantined == 1
    with pytest.raises(DecisionRecordUnavailableError, match="quarantined"):
        repository.load(Strategy.TOMORROW, expected.trade_date)
    assert next((tmp_path / "v2-decisions" / "quarantine").rglob("*.json")).is_file()


def test_invalid_manifest_path_is_quarantined_without_accessing_outside_root(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    expected = record()
    repository.commit(expected)
    database = tmp_path / "v2-decisions" / "v2-decisions.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE decision_records SET relative_path = '../outside.json' WHERE strategy = ?",
            (Strategy.TOMORROW.value,),
        )

    summary = repository.recover()

    assert summary.quarantined == 1
    with pytest.raises(DecisionRecordUnavailableError, match="quarantined"):
        repository.load(Strategy.TOMORROW, expected.trade_date)


def test_v2_checkpoint_round_trip_is_verified_and_consumed(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    boundary = NOW.replace(hour=14, minute=50)
    checkpoint = V2DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)

    repository.save_checkpoint(checkpoint)
    repository.save_checkpoint(checkpoint)

    assert repository.load_checkpoint(Strategy.TOMORROW, NOW.date()) == checkpoint
    repository.consume_checkpoint(checkpoint, consumed_at=boundary)
    assert repository.load_checkpoint(Strategy.TOMORROW, NOW.date()) is None


def test_concurrent_checkpoint_writers_retain_the_newest_observation(tmp_path: Path) -> None:
    first = SQLiteDecisionRecordRepository(tmp_path)
    second = SQLiteDecisionRecordRepository(tmp_path)
    first.initialize()
    boundary = NOW.replace(hour=14, minute=50)
    older = V2DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)
    newer = V2DecisionCheckpoint(
        replace(decision(sequence=3), observed_at=boundary - timedelta(seconds=10)),
        boundary,
    )

    def save(item) -> str:
        repository, checkpoint = item
        try:
            repository.save_checkpoint(checkpoint)
        except DecisionRecordConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(save, ((first, older), (second, newer))))

    assert "saved" in outcomes
    assert first.load_checkpoint(Strategy.TOMORROW, NOW.date()) == newer


def test_corrupted_checkpoint_is_quarantined_and_never_restored(tmp_path: Path) -> None:
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    boundary = NOW.replace(hour=14, minute=50)
    checkpoint = V2DecisionCheckpoint(replace(decision(), observed_at=boundary - timedelta(seconds=20)), boundary)
    repository.save_checkpoint(checkpoint)
    payload = next((tmp_path / "v2-decisions" / "checkpoints").rglob("*.json"))
    payload.write_bytes(b"corrupt")

    summary = repository.recover()

    assert summary.quarantined == 1
    assert repository.load_checkpoint(Strategy.TOMORROW, NOW.date()) is None
    assert next((tmp_path / "v2-decisions" / "quarantine" / "checkpoint_invalid").rglob("*.json")).is_file()
