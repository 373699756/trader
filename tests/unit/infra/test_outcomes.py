from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FormalScoredFreeze,
    FreezeDownside,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.infra.persistence.outcome_evidence_index import (
    OutcomeEvidenceConflictError,
    SQLiteOutcomeEvidenceIndex,
)
from trader.recommendation.domain.outcomes.models import BenchmarkReturn, OutcomeExitStatus, RecommendationOutcome


class _Freezes:
    def __init__(self, record: CommittedFreezeRecord | None = None) -> None:
        self.record = record

    def list_dates(self, strategy, *, limit=31):
        assert limit == 31
        if self.record is None or strategy is not self.record.strategy:
            return ()
        return (self.record.trade_date,)

    def load(self, strategy, trade_date):
        if self.record is not None and strategy is self.record.strategy and trade_date == self.record.trade_date:
            return self.record
        return None


def _repository(tmp_path: Path, record: CommittedFreezeRecord | None = None):
    return SQLiteOutcomeEvidenceIndex(tmp_path, _Freezes(record), sqlite_policy=SqlitePolicyFixture())


def _outcome(
    *,
    net_excess_return_pct: float = 1.2,
    settled_at=NOW,
    snapshot_id: str = "snapshot-fixture",
    strategy: Strategy = Strategy.TOMORROW,
    stock_code: str = "600001",
    horizon: int = 1,
) -> RecommendationOutcome:
    return RecommendationOutcome(
        snapshot_id=snapshot_id,
        strategy=strategy,
        recommend_date="2026-07-20",
        stock_code=stock_code,
        horizon=horizon,
        status="complete",
        settled_at=settled_at,
        anchor_raw_price=10.0,
        anchor_qfq_price=10.0,
        atr20_pct=2.5,
        minimum_qfq_low=9.8,
        end_qfq_close=10.2,
        exit_status=OutcomeExitStatus.TRADABLE,
        gross_return_pct=2.0,
        benchmark_return_pct=0.6,
        net_excess_return_pct=net_excess_return_pct,
        mae_pct=-2.0,
        mae_atr=-0.8,
        severe_drawdown=False,
    )


def test_pending_targets_are_derived_only_from_selected_formal_freezes(tmp_path: Path) -> None:
    original = freeze(Strategy.TOMORROW)
    item = original.items[0]
    formal = CommittedFreezeRecord(
        replace(
            original,
            items=(
                replace(
                    item,
                    selected=True,
                    rank=1,
                    downside=FreezeDownside("pass", (), 2.5, None, None),
                ),
            ),
        ),
        NOW,
        "scheduled",
    )
    repository = _repository(tmp_path, formal)

    targets = repository.pending_outcome_targets(limit=10)

    assert len(targets) == 1
    assert targets[0].snapshot_id == formal.version
    assert targets[0].anchor_raw_price == item.quote.price
    assert targets[0].atr20_pct == 2.5


def test_pending_target_does_not_fabricate_zero_atr_when_downside_is_unavailable(tmp_path: Path) -> None:
    original = freeze(Strategy.TOMORROW)
    item = original.items[0]
    formal = CommittedFreezeRecord(
        replace(original, items=(replace(item, selected=True, rank=1, downside=None),)),
        NOW,
        "scheduled",
    )
    repository = _repository(tmp_path, formal)

    targets = repository.pending_outcome_targets(limit=10)

    assert len(targets) == 1
    assert targets[0].atr20_pct is None


def test_recommend_and_watch_outcomes_are_counted_separately_from_avoided_leader(tmp_path: Path) -> None:
    original = freeze(Strategy.TOMORROW)
    base = original.items[0]
    assert base.quote is not None
    current = replace(
        original,
        items=(
            replace(
                base,
                code="600002",
                quote=replace(base.quote, code="600002"),
                selected=True,
                rank=1,
                action=RecommendationAction.EXECUTABLE,
            ),
            replace(
                base,
                code="600003",
                quote=replace(base.quote, code="600003"),
                selected=True,
                rank=2,
                action=RecommendationAction.OBSERVE,
            ),
            replace(
                base,
                code="600004",
                quote=replace(base.quote, code="600004"),
                selected=False,
                rank=0,
                selection_rank=0,
                action=RecommendationAction.UNAVAILABLE,
            ),
        ),
        selection_diagnostics=None,
    )
    formal = CommittedFreezeRecord(FormalScoredFreeze(current), NOW, "scheduled")
    repository = _repository(tmp_path, formal)

    assert {target.stock_code for target in repository.pending_outcome_targets(limit=10)} == {
        "600002",
        "600003",
    }
    repository.save_recommendation_outcomes(
        (
            _outcome(snapshot_id=formal.version, stock_code="600002", net_excess_return_pct=1.2),
            _outcome(snapshot_id=formal.version, stock_code="600003", net_excess_return_pct=-0.4),
        )
    )
    summaries = repository.summaries(Strategy.TOMORROW)
    assert [(item.tier, item.sample_count, item.complete_count, item.positive_count) for item in summaries] == [
        ("recommend", 1, 1, 1),
        ("watch", 1, 1, 0),
    ]
    assert [item.average_net_excess_pct for item in summaries] == [1.2, -0.4]


def test_benchmark_and_outcome_retries_ignore_observation_time_but_reject_business_conflicts(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    benchmark = BenchmarkReturn("2026-07-21", 0.6)
    repository.record_benchmark_return(benchmark, observed_at=NOW)
    repository.record_benchmark_return(benchmark, observed_at=NOW + timedelta(minutes=10))
    repository.save_recommendation_outcomes((_outcome(),))
    repository.save_recommendation_outcomes((_outcome(settled_at=NOW + timedelta(minutes=10)),))

    assert repository.benchmark_returns_after("2026-07-20", limit=1) == (benchmark,)
    with pytest.raises(OutcomeEvidenceConflictError, match="benchmark return"):
        repository.record_benchmark_return(BenchmarkReturn("2026-07-21", 0.7), observed_at=NOW)
    with pytest.raises(OutcomeEvidenceConflictError, match="recommendation outcome"):
        repository.save_recommendation_outcomes((_outcome(net_excess_return_pct=1.3),))


def test_retryable_outcome_cannot_occupy_immutable_horizon(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    benchmark_missing = replace(
        _outcome(),
        status="benchmark_missing",
        benchmark_return_pct=None,
        net_excess_return_pct=None,
        quality_reason="benchmark_missing",
    )
    horizon_not_due = replace(benchmark_missing, status="insufficient_data", quality_reason="horizon_not_due")

    for retryable in (benchmark_missing, horizon_not_due):
        with pytest.raises(ValueError, match="retryable recommendation outcomes"):
            repository.save_recommendation_outcomes((retryable,))

    assert not (tmp_path / "outcomes.sqlite3").exists()


def test_outcome_schema_and_payload_are_durable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.initialize()
    repository.save_recommendation_outcomes((_outcome(),))

    database = tmp_path / "outcomes.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        row = connection.run("SELECT status, record_hash, record_bytes FROM recommendation_outcomes").fetchone()

    assert row is not None
    assert row[0] == "complete"
    assert len(row[1]) == 32
    payload = json.loads(row[2])
    assert payload["anchor_raw_price"] == 10.0
    assert payload["anchor_qfq_price"] == 10.0
    assert payload["minimum_qfq_low"] == 9.8
    assert payload["end_qfq_close"] == 10.2
    assert payload["exit_status"] == "tradable"
    assert payload["exit_untradable"] is False
    assert "anchor_price" not in payload
    assert "minimum_low" not in payload
    assert "end_close" not in payload
    status = SQLiteOutcomeEvidenceIndex.inspect_status(tmp_path, sqlite_policy=SqlitePolicyFixture())
    assert status.initialized is True
    assert status.recommendation_outcomes == 1
    assert status.complete_outcomes == 1


def test_complete_return_without_atr_risk_label_remains_durable_and_counted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    outcome = replace(
        _outcome(),
        atr20_pct=None,
        mae_atr=None,
        severe_drawdown=None,
        quality_reason="atr20_unavailable",
    )

    repository.save_recommendation_outcomes((outcome,))

    database = tmp_path / "outcomes.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        row = connection.run("SELECT status, record_bytes FROM recommendation_outcomes").fetchone()
    assert row is not None
    payload = json.loads(row[1])
    assert row[0] == "complete"
    assert payload["atr20_pct"] is None
    assert payload["mae_atr"] is None
    assert payload["severe_drawdown"] is None
    assert payload["quality_reason"] == "atr20_unavailable"
    assert SQLiteOutcomeEvidenceIndex.inspect_status(tmp_path, sqlite_policy=SqlitePolicyFixture()).complete_outcomes == 1


def test_outcome_status_inspection_is_read_only_for_missing_database(tmp_path: Path) -> None:
    status = SQLiteOutcomeEvidenceIndex.inspect_status(tmp_path, sqlite_policy=SqlitePolicyFixture())

    assert status.initialized is False
    assert status.benchmark_returns == 0
    assert not (tmp_path / "outcomes.sqlite3").exists()


def test_benchmark_read_rejects_tampered_columns(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.record_benchmark_return(BenchmarkReturn("2026-07-21", 0.6), observed_at=NOW)
    database = tmp_path / "outcomes.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        connection.run("UPDATE benchmark_returns SET return_pct = 9.9")

    with pytest.raises(OutcomeEvidenceConflictError, match="benchmark return"):
        repository.benchmark_returns_after("2026-07-20", limit=1)


def test_legacy_d25_outcomes_resume_with_only_t4_pending(tmp_path: Path) -> None:
    original = freeze(Strategy.D25)
    item = original.items[0]
    formal = CommittedFreezeRecord(
        replace(original, items=(replace(item, selected=True, rank=1),)),
        NOW,
        "scheduled",
    )
    repository = _repository(tmp_path, formal)
    repository.save_recommendation_outcomes(
        tuple(
            _outcome(
                snapshot_id=formal.version,
                strategy=Strategy.D25,
                stock_code=item.code,
                horizon=horizon,
            )
            for horizon in (2, 3, 5)
        )
    )

    targets = repository.pending_outcome_targets(limit=10)

    assert len(targets) == 1
    assert targets[0].pending_horizons == (4,)

    repository.save_recommendation_outcomes(
        (
            _outcome(
                snapshot_id=formal.version,
                strategy=Strategy.D25,
                stock_code=item.code,
                horizon=4,
            ),
        )
    )

    assert repository.pending_outcome_targets(limit=10) == ()
