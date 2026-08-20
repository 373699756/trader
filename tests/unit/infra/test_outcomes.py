from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.unit.domain.test_decision_identity import NOW, decision
from trader.application.ports.data_plane import HistoricalFeatureRecord
from trader.domain.outcome.models import BenchmarkReturn, RecommendationOutcome
from trader.domain.recommendation.decision_identity import CommittedDecisionRecord
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.outcomes import OutcomeEvidenceConflictError, SQLiteOutcomeEvidenceRepository


class _Decisions:
    def __init__(self, record: CommittedDecisionRecord | None = None) -> None:
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


class _Historical:
    def load_historical_feature_recent(self, code, trade_date):
        return HistoricalFeatureRecord(
            code=code,
            trade_date=trade_date,
            data_version="history:v1",
            source="fixture",
            source_time=NOW,
            observed_at=NOW,
            payload={"history_summary": {"profile": {"atr20_pct": 2.5}}},
        )


def _repository(tmp_path: Path, record: CommittedDecisionRecord | None = None):
    return SQLiteOutcomeEvidenceRepository(tmp_path, _Decisions(record), _Historical())


def _outcome(*, net_excess_return_pct: float = 1.2, settled_at=NOW) -> RecommendationOutcome:
    return RecommendationOutcome(
        snapshot_id="snapshot:v1",
        strategy=Strategy.TOMORROW,
        recommend_date="2026-07-20",
        stock_code="600001",
        horizon=1,
        status="complete",
        settled_at=settled_at,
        anchor_price=10.0,
        atr20_pct=2.5,
        minimum_low=9.8,
        end_close=10.2,
        gross_return_pct=2.0,
        benchmark_return_pct=0.6,
        net_excess_return_pct=net_excess_return_pct,
        mae_pct=-2.0,
        mae_atr=-0.8,
        severe_drawdown=False,
    )


def test_pending_targets_are_derived_only_from_selected_formal_decisions(tmp_path: Path) -> None:
    original = decision(Strategy.TOMORROW)
    item = original.items[0]
    formal = CommittedDecisionRecord(
        replace(original, items=(replace(item, selected=True, rank=1),)),
        NOW,
        "scheduled",
    )
    repository = _repository(tmp_path, formal)

    targets = repository.pending_outcome_targets(limit=10)

    assert len(targets) == 1
    assert targets[0].snapshot_id == formal.version
    assert targets[0].anchor_price == item.quote.price
    assert targets[0].atr20_pct == 2.5


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


def test_outcome_schema_and_payload_are_durable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.initialize()
    repository.save_recommendation_outcomes((_outcome(),))

    database = tmp_path / "research" / "outcomes.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT status, payload_hash, length(payload) FROM recommendation_outcomes").fetchone()

    assert row is not None
    assert row[0] == "complete"
    assert len(row[1]) == 64
    assert row[2] > 0
    status = SQLiteOutcomeEvidenceRepository.inspect_status(tmp_path)
    assert status.initialized is True
    assert status.recommendation_outcomes == 1
    assert status.complete_outcomes == 1


def test_outcome_status_inspection_is_read_only_for_missing_database(tmp_path: Path) -> None:
    status = SQLiteOutcomeEvidenceRepository.inspect_status(tmp_path)

    assert status.initialized is False
    assert status.benchmark_returns == 0
    assert not (tmp_path / "research").exists()


def test_benchmark_read_rejects_tampered_columns(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.record_benchmark_return(BenchmarkReturn("2026-07-21", 0.6), observed_at=NOW)
    database = tmp_path / "research" / "outcomes.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE benchmark_returns SET return_pct = 9.9")

    with pytest.raises(OutcomeEvidenceConflictError, match="benchmark return"):
        repository.benchmark_returns_after("2026-07-20", limit=1)
