"""Formal D25 settlement keeps the target and the outcome independently readable."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from trader.http_api.response.freeze_projection import SerializeFreezeView
from trader.recommendation.application.d25_outcome_settlement import D25OutcomeSettlementService
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import FreezeQuerySources, UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.outcomes.models import D25CloseEvidence
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord, FreezeModelDiagnostics
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from tests.helpers.recommendation.publication.freeze_identity import NOW, freeze
from tests.helpers.shared.configuration import SqlitePolicyFixture


def test_frozen_d25_target_settles_without_any_benchmark_rows(tmp_path):
    policy = SqlitePolicyFixture()
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3", sqlite_policy=policy)
    records.initialize()
    target = PredictionTarget(Strategy.D25, 100)
    candidate = freeze(Strategy.D25)
    item = candidate.items[0]
    close_at = NOW.replace(hour=15, minute=0)
    committed = replace(
        candidate,
        observed_at=close_at,
        is_formal=True,
        input_versions=(
            ("native", "published-input"),
            ("prediction_target", target.identity),
            ("d25_close_evidence", "official-close:published-input"),
        ),
        items=(replace(
            item,
            quote=replace(item.quote, price=100.0, source_time=close_at),
            model_diagnostics=FreezeModelDiagnostics(80.0, 1.5, 0.2, 1.3, target_probability=0.68),
        ),),
    )
    records.commit(CommittedFreezeRecord(committed, close_at, "scheduled"))
    index = SQLiteOutcomeEvidenceIndex(tmp_path / "research", records, sqlite_policy=policy)
    last_day = close_at + timedelta(days=7)

    class Market:
        def read_d25_close_evidence(self, targets, observed_at):
            assert [item.prediction_target for item in targets] == [target.identity]
            if observed_at < last_day:
                return ()
            return (D25CloseEvidence(
                "600001", close_at.date(),
                (close_at.date() + timedelta(days=2), close_at.date() + timedelta(days=3),
                 close_at.date() + timedelta(days=6), last_day.date()),
                "published-close-prices", Decimal("100"), Decimal("100"),
                (Decimal("100"), Decimal("101"), Decimal("101"), Decimal("102")),
            ),)

    class Clock:
        def now(self):
            return last_day

    service = D25OutcomeSettlementService(Market(), index)
    queries = UnifiedFreezeQueries(
        UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), records, Clock(), FreezeQuerySources(outcome_summaries=index)
    )
    before = queries.history(Strategy.D25, close_at.date())
    assert SerializeFreezeView(before)["items"][0]["d25_outcome"] == "pending"
    assert service.settle(last_day - timedelta(minutes=1)).settled == 0
    assert service.settle(last_day).settled == 1
    assert service.settle(last_day).settled == 0
    after = queries.history(Strategy.D25, close_at.date())
    result = SerializeFreezeView(after)
    assert result["items"][0]["d25_outcome"] == "hit"
    assert result["items"][0]["actual_return_pct"] == 1.0
    assert result["items"][0]["scores"]["target_probability"] == 0.68
    assert result["outcome_summaries"][0]["hit_count"] == 1
    assert before.etag != after.etag
