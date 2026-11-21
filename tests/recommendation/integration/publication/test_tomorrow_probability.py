from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from tests.helpers.recommendation.publication.freeze_identity import NOW, freeze
from tests.helpers.recommendation.publication.tomorrow_projection import EVALUATED_AT, PROJECT_ROOT, _native_input, _verified_feature
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.http_api.response.freeze_projection import SerializeFreezeView
from trader.infra.sqlite_codec import EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnectionOptions, SqliteSession, SqliteTransaction
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
from trader.recommendation.application.ports.freeze_records import FreezeRecordConflictError, FreezeRecordUnavailableError
from trader.recommendation.application.tomorrow_outcome_settlement import TomorrowOutcomeSettlementService
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.models import Board, Evidence
from trader.recommendation.domain.market.tomorrow_probability import BuildTomorrowFeatures
from trader.recommendation.domain.outcomes.models import TomorrowCloseEvidence
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.outcomes.tomorrow_target import TOMORROW_CLOSE_GAIN_TARGET
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import TOMORROW_PROBABILITY_SCORE_SCALE
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords, _GROUPED_SCHEMA
from trader.recommendation.infra.persistence.outcome_evidence_index import SQLiteOutcomeEvidenceIndex
from trader.recommendation.infra.persistence.strategy_freeze_records import StrategyFreezeRecords
from trader.recommendation.infra.policy_settings import BuildRecommendationPolicy
from trader.recommendation.infra.settings.loading import LoadStrategySettings


def _current(*, close_evidence: bool, quote_time, target=None, profile=None):
    current = freeze()
    versions = (
        ("native", "published_input"),
        ("score_scale", TOMORROW_PROBABILITY_SCORE_SCALE),
        ("score_model", "model_digest"),
        ("score_model_id", "model_digest" if target is not None else TOMORROW_CLOSE_GAIN_TARGET),
        ("prediction_target", target.identity if target is not None else TOMORROW_CLOSE_GAIN_TARGET),
        ("score_contract_hash", "model_digest"),
    )
    if close_evidence:
        versions += (("tomorrow_close_evidence", "official-close:published_input"),)
    if profile is not None:
        versions += (("scoring_profile", profile),)
    item = current.items[0]
    assert item.quote is not None
    return replace(
        current,
        observed_at=quote_time.replace(hour=15, minute=1),
        trade_date=quote_time.date(),
        input_versions=versions,
        items=(replace(item, quote=replace(item.quote, source_time=quote_time, price=100.0), final_score=25.0),),
    )


def test_formal_tomorrow_rejects_an_intraday_quote_even_with_a_claimed_close():
    current = _current(close_evidence=True, quote_time=NOW, target=PredictionTarget(Strategy.TOMORROW, 800))
    with pytest.raises(ValueError, match="closing evidence"):
        replace(current, is_formal=True)


def test_independent_tomorrow_freezes_preserve_read_only_grouped_history_and_its_hit_rate(tmp_path):
    policy = SqlitePolicyFixture()
    shared = SQLiteFreezeRecords(tmp_path / "shared" / "freeze.sqlite3", sqlite_policy=policy)
    grouped_path = tmp_path / "shared" / "targets.sqlite3"
    grouped = SQLiteFreezeRecords(grouped_path, sqlite_policy=policy, grouped=True)
    profile_a = SQLiteFreezeRecords(tmp_path / "v2" / "freeze.sqlite3", sqlite_policy=policy)
    profile_b = SQLiteFreezeRecords(tmp_path / "v3" / "freeze.sqlite3", sqlite_policy=policy)
    target = PredictionTarget(Strategy.TOMORROW, 500)
    owner_a = StrategyFreezeRecords(profile_a, shared, grouped, "v2", target, owns_legacy_settlement=True)
    owner_b = StrategyFreezeRecords(profile_b, shared, grouped, "v3", target)
    owner_a.initialize()
    owner_b.initialize()
    assert not grouped_path.exists()
    close = NOW.replace(hour=15, minute=0)
    legacy = _current(close_evidence=True, quote_time=close, target=target)
    legacy_record = CommittedFreezeRecord(replace(legacy, is_formal=True), legacy.observed_at, "close_fallback")
    legacy_bytes = CommittedRecordBytes(legacy_record)
    grouped_path.parent.mkdir()
    with (
        SqliteSession(grouped_path, SqliteConnectionOptions(), sqlite_policy=policy) as connection,
        SqliteTransaction(connection, immediate=True),
    ):
        EnsureSqliteSchema(connection, _GROUPED_SCHEMA, allow_create=True, exact=True)
        connection.run(
            "INSERT INTO freeze_record VALUES (?,?,?,?,?,?,?,?,'committed',NULL,?)",
            (
                Strategy.TOMORROW.value, EncodeDate(legacy_record.trade_date), legacy_record.version,
                EncodeHash(legacy_record.payload_hash), sha256(legacy_bytes).digest(), legacy_bytes,
                EncodeTime(legacy_record.committed_at), legacy_record.commit_kind, target.identity,
            ),
        )
        connection.run(
            "INSERT INTO tomorrow_profile_day VALUES (?,?,?),(?,?,?)",
            ("v2", EncodeDate(legacy_record.trade_date), legacy_record.version,
             "v3", EncodeDate(legacy_record.trade_date), legacy_record.version),
        )
    archived_database = grouped_path.read_bytes()
    with pytest.raises(FreezeRecordUnavailableError, match="read-only"):
        grouped.initialize()
    assert owner_a.load(Strategy.TOMORROW, close.date()) == legacy_record
    assert owner_b.load_history(Strategy.TOMORROW, close.date()).version == legacy_record.version

    current = _current(close_evidence=True, quote_time=close, target=target, profile="v2")
    with pytest.raises(FreezeRecordConflictError):
        owner_a.commit(CommittedFreezeRecord(replace(current, is_formal=True), current.observed_at, "close_fallback"))
    next_close = close + timedelta(days=1)
    records = []
    for lane, profile in ((owner_a, "v2"), (owner_b, "v3")):
        current = _current(close_evidence=True, quote_time=next_close, target=target, profile=profile)
        record = CommittedFreezeRecord(replace(current, is_formal=True), current.observed_at, "close_fallback")
        lane.commit(record)
        assert lane.load(Strategy.TOMORROW, next_close.date()) == record
        assert lane.load_history(Strategy.TOMORROW, next_close.date()).version == record.version
        records.append(record)
    assert len({item.version for item in records}) == len(records)
    assert owner_a.tomorrow_settlement_records(close.date()) == (legacy_record,)
    assert owner_b.tomorrow_settlement_records(close.date()) == ()
    assert grouped_path.read_bytes() == archived_database

    shared_outcome_path = tmp_path / "research" / "tomorrow" / "outcomes.sqlite3"
    first = SQLiteOutcomeEvidenceIndex(
        tmp_path / "research" / "v2", owner_a, sqlite_policy=policy, tomorrow_database=shared_outcome_path,
        tomorrow_settlement_records=owner_a.tomorrow_settlement_records,
    )
    second = SQLiteOutcomeEvidenceIndex(
        tmp_path / "research" / "v3", owner_b, sqlite_policy=policy, tomorrow_database=shared_outcome_path,
        tomorrow_settlement_records=owner_b.tomorrow_settlement_records,
    )

    class Market:
        publication_available = False

        def read_tomorrow_close_evidence(self, targets, observed_at):
            if not self.publication_available:
                return ()
            assert observed_at.date() == next_close.date() + timedelta(days=1)
            return tuple(
                TomorrowCloseEvidence(
                    "600001", day, day + timedelta(days=1), "publication-after-close",
                    Decimal("100"), Decimal("100"), Decimal("105"), False, Decimal("100"), Decimal("105"),
                )
                for day in (close.date(), next_close.date())
            )

    market = Market()
    service = TomorrowOutcomeSettlementService(market, first)
    observed = next_close + timedelta(days=1)

    class Clock:
        def now(self):
            return observed

    queries = UnifiedFreezeQueries(
        UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), owner_b, Clock(),
        FreezeQuerySources(outcome_summaries=second),
    )
    before = queries.history(Strategy.TOMORROW, close.date())
    assert SerializeFreezeView(before)["items"][0]["tomorrow_outcome"] == "pending"
    assert service.settle(observed).settled == 0
    market.publication_available = True
    assert service.settle(observed).settled == 2
    assert service.settle(observed).settled == 0
    assert TomorrowOutcomeSettlementService(market, second).settle(observed).settled == 1
    recommend, selected = second.summaries(Strategy.TOMORROW)
    assert recommend.prediction_target == target.identity
    assert (recommend.sample_count, recommend.complete_count, recommend.hit_count) == (2, 2, 2)
    assert (selected.sample_count, selected.complete_count, selected.hit_count) == (2, 2, 2)
    for record in (*records, legacy_record):
        hit, = second.target_outcomes(record.version, Strategy.TOMORROW, target.identity)
        assert (hit.code, hit.label, hit.actual_return_pct) == ("600001", "hit", 5.0)
    after = queries.history(Strategy.TOMORROW, close.date())
    assert SerializeFreezeView(after)["items"][0]["tomorrow_outcome"] == "hit"
    assert SerializeFreezeView(after)["items"][0]["actual_return_pct"] == 5.0
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
