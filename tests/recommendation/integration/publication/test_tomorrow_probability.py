from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.helpers.recommendation.publication.freeze_identity import NOW, freeze
from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, PROJECT_ROOT, _native_input, _verified_feature
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.http_api.response.freeze_projection import SerializeFreezeView
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredLocal,
    ScoredProjectionInputs,
    _official_close_evidence,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import FreezeQuerySources, UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.tomorrow_probability import TomorrowProbabilityCapability
from trader.recommendation.application.tomorrow_outcome_settlement import TomorrowOutcomeSettlementService
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.models import Board, Evidence
from trader.recommendation.domain.market.tomorrow_probability import BuildTomorrowFeatures
from trader.recommendation.domain.outcomes.models import TomorrowCloseEvidence
from trader.recommendation.domain.outcomes.tomorrow_target import TOMORROW_CLOSE_GAIN_TARGET
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import TOMORROW_PROBABILITY_SCORE_SCALE
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.recommendation.infra.persistence.strategy_freeze_records import StrategyFreezeRecords
from trader.recommendation.infra.policy_settings import BuildRecommendationPolicy
from trader.recommendation.infra.settings.loading import LoadStrategySettings


def _current(*, close_evidence: bool, quote_time):
    current = freeze()
    versions = (
        ("native", "published_input"),
        ("score_scale", TOMORROW_PROBABILITY_SCORE_SCALE),
        ("score_model", "model_digest"),
        ("score_model_id", TOMORROW_CLOSE_GAIN_TARGET),
        ("prediction_target", TOMORROW_CLOSE_GAIN_TARGET),
        ("score_contract_hash", "model_digest"),
    )
    if close_evidence:
        versions += (("tomorrow_close_evidence", "official-close:published_input"),)
    item = current.items[0]
    assert item.quote is not None
    return replace(
        current,
        observed_at=NOW.replace(hour=15, minute=1),
        input_versions=versions,
        items=(replace(item, quote=replace(item.quote, source_time=quote_time, price=100.0), final_score=25.0),),
    )


def test_formal_tomorrow_rejects_an_intraday_quote_even_with_a_claimed_close():
    current = _current(close_evidence=True, quote_time=NOW)
    with pytest.raises(ValueError, match="closing evidence"):
        replace(current, is_formal=True)


def test_profile_switch_preserves_one_frozen_tomorrow_and_its_hit_rate(tmp_path):
    policy = SqlitePolicyFixture()
    shared = SQLiteFreezeRecords(tmp_path / "shared" / "freeze.sqlite3", sqlite_policy=policy)
    profile_a = SQLiteFreezeRecords(tmp_path / "v2" / "freeze.sqlite3", sqlite_policy=policy)
    profile_b = SQLiteFreezeRecords(tmp_path / "v3" / "freeze.sqlite3", sqlite_policy=policy)
    owner_a = StrategyFreezeRecords(profile_a, shared, True)
    owner_b = StrategyFreezeRecords(profile_b, shared, True)
    owner_a.initialize()
    owner_b.initialize()
    current = _current(close_evidence=True, quote_time=NOW.replace(hour=15, minute=0))
    record = CommittedFreezeRecord(replace(current, is_formal=True), current.observed_at, "close_fallback")
    owner_a.commit(record)
    assert owner_b.load(Strategy.TOMORROW, NOW.date()) == record

    next_day = NOW.date() + timedelta(days=1)
    shared_outcome_path = tmp_path / "research" / "tomorrow" / "outcomes.sqlite3"
    first = SQLiteOutcomeEvidenceIndex(
        tmp_path / "research" / "v2", owner_a, sqlite_policy=policy, tomorrow_database=shared_outcome_path,
    )
    second = SQLiteOutcomeEvidenceIndex(
        tmp_path / "research" / "v3", owner_b, sqlite_policy=policy, tomorrow_database=shared_outcome_path,
    )

    class Market:
        publication_available = False

        def read_tomorrow_close_evidence(self, targets, observed_at):
            assert targets[0].snapshot_id == record.version
            assert observed_at.date() == next_day
            if not self.publication_available:
                return ()
            return (
                TomorrowCloseEvidence(
                    "600001", NOW.date(), next_day, "publication-after-close",
                    Decimal("100"), Decimal("100"), Decimal("105"), False, Decimal("100"), Decimal("105"),
                ),
            )

    market = Market()
    service = TomorrowOutcomeSettlementService(market, first)
    observed = current.observed_at + timedelta(days=1)

    class Clock:
        def now(self):
            return observed

    queries = UnifiedFreezeQueries(
        UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), owner_b, Clock(),
        FreezeQuerySources(outcome_summaries=second),
    )
    before = queries.history(Strategy.TOMORROW, NOW.date())
    assert SerializeFreezeView(before)["items"][0]["tomorrow_outcome"] == "pending"
    assert service.settle(observed).settled == 0
    market.publication_available = True
    assert service.settle(observed).settled == 1
    assert service.settle(observed).settled == 0
    recommend, selected = second.summaries(Strategy.TOMORROW)
    assert recommend.prediction_target == TOMORROW_CLOSE_GAIN_TARGET
    assert (recommend.sample_count, recommend.complete_count, recommend.hit_count) == (1, 1, 1)
    assert (selected.sample_count, selected.complete_count, selected.hit_count) == (1, 1, 1)
    assert second.tomorrow_hits(record.version) == (("600001", "hit"),)
    after = queries.history(Strategy.TOMORROW, NOW.date())
    assert SerializeFreezeView(after)["items"][0]["tomorrow_outcome"] == "hit"
    assert before.etag != after.etag


def test_probability_feature_contract_distinguishes_actual_volume_and_invalid_bars():
    assert BuildTomorrowFeatures(10.0, 10.0, 10.0, 10.0, 500.0, Board.MAIN)[2] == 0.5
    assert BuildTomorrowFeatures(10.0, 10.0, 10.0, 11.0, 500.0, Board.MAIN) is None
    assert BuildTomorrowFeatures(10.0, 10.0, 10.0, 10.0, 0.0, Board.MAIN) is None


def test_scoring_uses_shared_probability_to_rank_without_a_net_return_gate(application_feature_factory):
    class Predictor:
        identity = "a" * 64

        def predict_probabilities(self, rows):
            return tuple(0.10 + index * 0.10 for index in range(len(rows)))

    source_time = EVALUATED_AT - timedelta(seconds=5)
    features = []
    for index in range(3):
        feature = _verified_feature(application_feature_factory(f"60000{index}", source_time))
        features.append(
            replace(
                feature, history_days=61,
                quote=replace(feature.quote, volume=1_000_000.0),
                model_history_quality=ModelHistoryQuality("history_window_insufficient"),
            )
        )
    policy = BuildRecommendationPolicy(LoadStrategySettings(PROJECT_ROOT / "config" / "strategy.json"))
    policy = replace(policy, selection=replace(policy.selection, thresholds={"tomorrow": 0.0, "d25": 0.0}))
    scorer = ModelScoringRouter("v2", {Strategy.TOMORROW: TomorrowProbabilityCapability("v2", Predictor())})
    projection = BuildScoredLocal(
        _native_input(tuple(features)),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=scorer),
    )
    ranked = sorted((item for item in projection.local.items if item.selected), key=lambda item: item.rank)
    assert [item.code for item in ranked] == ["600002", "600001", "600000"], projection.local.filter_aggregates
    assert [item.final_score for item in ranked] == pytest.approx([30.0, 20.0, 10.0])
    assert all(item.model_diagnostics is None for item in ranked)
    assert dict(projection.local.input_versions)["prediction_target"] == TOMORROW_CLOSE_GAIN_TARGET


def test_close_allows_final_quote_compilation_but_rejects_late_company_evidence(application_feature_factory):
    original = application_feature_factory("600001", EVALUATED_AT)
    native = _native_input((original,))
    cutoff = EVALUATED_AT.replace(hour=15, minute=0)
    received = cutoff + timedelta(seconds=5)
    quote = replace(original.quote, source_time=cutoff, received_time=received)
    evidence = Evidence("market", "structured_point_in_time", "closing quote", "fixture", received, received)
    final = replace(original, quote=quote, observed_at=received, evidence=(evidence,))
    native = replace(
        native, phase="close_fallback", evaluated_at=received, market_features=(final,), candidate_features=(final,),
    )
    assert _official_close_evidence(native, (final,)) == f"official-close:{native.input_version}"
    late_filing = replace(evidence, evidence_type="regulatory_filing")
    contaminated = replace(final, evidence=(evidence, late_filing))
    assert _official_close_evidence(native, (contaminated,)) is None
