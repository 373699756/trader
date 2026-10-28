"""V5 engineering freezes stay observable without acquiring formal authority."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.application.scoring_helpers import profile_for
from tests.unit.application.test_input_runtime import _policy
from tests.unit.application.test_tomorrow_production_model import _model_feature
from tests.unit.application.test_tomorrow_projection import EVALUATED_AT, _native_input, _verified_feature
from tests.unit.application.test_v5_model_filtering import _SequencePredictor
from tests.unit.domain.test_freeze_identity import NOW, freeze
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredLocal,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    FreezeRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.runtime_adapters import DeepSeekAdapter
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.pipeline.local_score.unavailable_model import UnavailableModelCapability
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.market.v5.features import ModelPriceVolumeSequence
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FormalScoredFreeze,
    IsShadowFreeze,
    ScoredFreeze,
    VisibleScoredSeats,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


def _shadow(strategy: Strategy, now: datetime) -> ScoredFreeze:
    source = freeze(strategy)
    return replace(
        source,
        observed_at=now,
        input_versions=(
            *source.input_versions,
            ("score_scale", "weighted_evidence_quality_0_100"),
            ("score_model", "v5_industry_tcn:" + "a" * 64),
            ("execution_mode", "shadow"),
            ("production_authority", "false"),
        ),
        items=tuple(replace(item, action=RecommendationAction.OBSERVE, quote=None) for item in source.items),
    )


def _freezer(index, records, clock, strategy, *, production_authority=False):
    return ScoredFreezeCoordinator(
        index,
        records,
        clock,
        runtime_identity=FreezeRuntimeIdentity("config-current", "strategy-current", "fusion-current"),
        strategy=strategy,
        production_authority=production_authority,
    )


def _features(factory):
    dates = tuple(EVALUATED_AT.date() - timedelta(days=61 - index) for index in range(61))
    features = []
    for index in range(100):
        code = f"600{index:03d}"
        source = _verified_feature(factory(code, EVALUATED_AT - timedelta(seconds=10)))
        source = _model_feature(source, offset=index / 1000.0, amihud=1.0)
        features.append(
            replace(
                source,
                model_sequence=ModelPriceVolumeSequence(code, dates, (0.01,) * 360, "v5-fixture"),
                values={**source.values, "entry_quality": 95.0 - index / 100.0},
            )
        )
    return tuple(features)


def _model_router(strategy, *, production_authority=False):
    predictor = _SequencePredictor()
    predictor.calls = 0
    if strategy is Strategy.D25:
        predictor.feature_ids = predictor.feature_ids[1:]
    profile = profile_for(predictor, strategy)
    head = profile.heads[strategy]
    profile = replace(
        profile,
        heads={strategy: replace(head, evidence=replace(head.evidence, production_authority=production_authority))},
    )
    service = ProductionModelScoringService(profile, strategy, cost_policy=ExecutionCostPolicy(0.002, 1.0))
    return ModelScoringRouter("v5", {strategy: service}), predictor


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize("phase", ("final_review", "close_fallback"))
def test_v5_local_and_model_free_close_rebuild_keep_shadow_actions_and_local_scores(
    application_feature_factory, native_type, phase
) -> None:
    strategy = native_type.strategy
    policy = _policy()
    weights = {
        board: {key: float(key == "entry_quality") for key in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(policy, board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights})
    router, predictor = _model_router(strategy)
    native = replace(_native_input(_features(application_feature_factory), native_type), phase=phase)

    projection = BuildScoredLocal(native, policy, sequence=1, runtime=ScoredProjectionInputs(model_scoring=router))

    seats = VisibleScoredSeats(projection.local)
    assert seats
    assert IsShadowFreeze(projection.local)
    assert projection.production_authority is False
    assert dict(projection.local.input_versions)["production_authority"] == "false"
    assert projection.score_scale == "weighted_evidence_quality_0_100"
    assert all(item.final_score == item.local_score for item in projection.local.items)
    assert all(item.action is not RecommendationAction.EXECUTABLE for item in projection.local.items)
    assert projection.review_candidates == ()
    assert all(reason.code != "deepseek_pending" for reason in projection.stages.risk_review.snapshot.reasons)
    assert predictor.calls == (0 if phase == "close_fallback" else 1)
    if phase == "close_fallback":
        assert projection.input_quality.history_required_sessions == 20
    # The adapter must skip before accessing any of these absent production dependencies.
    assert DeepSeekAdapter(None, None, None, now=lambda: EVALUATED_AT).build_hybrid(projection.local, None) is None


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_v5_missing_model_close_rebuild_cannot_gain_production_authority(application_feature_factory, native_type):
    strategy = native_type.strategy
    router = ModelScoringRouter("v5", {strategy: UnavailableModelCapability("v5", 62)})
    native = replace(_native_input(_features(application_feature_factory), native_type), phase="close_fallback")

    projection = BuildScoredLocal(native, _policy(), sequence=1, runtime=ScoredProjectionInputs(model_scoring=router))

    assert IsShadowFreeze(projection.local)
    assert projection.production_authority is False
    assert all(item.action is not RecommendationAction.EXECUTABLE for item in projection.local.items)


@pytest.mark.parametrize("upgrade", ("authority", "formal", "executable"))
def test_v5_weighted_score_shadow_identity_rejects_authority_upgrades(upgrade):
    shadow = _shadow(Strategy.TOMORROW, NOW)
    with pytest.raises(ValueError, match="shadow"):
        if upgrade == "authority":
            replace(
                shadow,
                input_versions=tuple(
                    (key, "true" if key == "production_authority" else value) for key, value in shadow.input_versions
                ),
            )
        elif upgrade == "formal":
            replace(shadow, is_formal=True)
        else:
            replace(shadow, items=(replace(shadow.items[0], action=RecommendationAction.EXECUTABLE),))


@pytest.mark.parametrize("hour,minute", ((10, 0), (12, 30), (14, 59), (15, 0), (15, 10)))
@pytest.mark.parametrize("cold", (False, True))
def test_v5_shadow_five_time_windows_never_checkpoint_or_freeze(tmp_path: Path, hour, minute, cold):
    now = NOW.replace(hour=hour, minute=minute)
    shadow = _shadow(Strategy.TOMORROW, now)
    records = SQLiteFreezeRecords(tmp_path / "v5" / "freeze.sqlite3")
    records.initialize()
    index = UnifiedFreezeIndex()
    if not cold:
        assert index.publish(shadow, expected_version=None).accepted
    freezer = _freezer(index, records, _Clock(now), Strategy.TOMORROW)

    assert freezer.freeze_scheduled().status == ("before_freeze" if hour < 15 else "shadow_only")
    assert freezer.freeze_close_fallback(
        shadow, recovery_path="close_rebuild", official_close_version="official-close:fixture"
    ).status == ("before_close_recovery" if hour < 15 else "shadow_only")
    assert records.load(Strategy.TOMORROW, now.date()) is None
    assert records.list_dates(Strategy.TOMORROW) == ()
    assert not index.is_sealed(Strategy.TOMORROW, now.date())
    with pytest.raises(ValueError, match="shadow"):
        FormalScoredFreeze(shadow)
    with pytest.raises(ValueError, match="shadow"):
        CommittedFreezeRecord(shadow, max(now, now.replace(hour=15, minute=0)), "scheduled")


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_v5_cold_start_cannot_restore_old_checkpoint_or_freeze_unmarked_rebuild(tmp_path: Path, strategy):
    now = NOW.replace(hour=15, minute=5)
    old = replace(freeze(strategy), observed_at=now.replace(hour=14, minute=59, second=50))
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3")
    records.initialize()
    index = UnifiedFreezeIndex()
    freezer = _freezer(index, records, _Clock(now), strategy)

    assert freezer.freeze_scheduled().status == "shadow_only"
    assert index.snapshot(strategy).current is None
    assert (
        freezer.freeze_close_fallback(
            replace(old, observed_at=now),
            recovery_path="close_rebuild",
            official_close_version="official-close:fixture",
        ).status
        == "shadow_only"
    )
    assert not index.is_sealed(strategy, now.date())
    assert records.load(strategy, now.date()) is None


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_v5_existing_formal_record_remains_readable_without_new_freeze_authority(tmp_path: Path, strategy):
    now = NOW.replace(hour=15, minute=5)
    old = FormalScoredFreeze(freeze(strategy))
    formal = CommittedFreezeRecord(old, now.replace(minute=0), "scheduled")
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3")
    records.initialize()
    records.commit(formal)
    index = UnifiedFreezeIndex()
    clock = _Clock(now)
    freezer = _freezer(index, records, clock, strategy)

    assert freezer.restore(now.date()).record == formal
    assert freezer.freeze_scheduled().record == formal
    assert (
        freezer.freeze_close_fallback(
            _shadow(strategy, now), recovery_path="close_rebuild", official_close_version="official-close:later"
        ).record
        == formal
    )
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), records, clock)
    history = queries.history(strategy, now.date())
    assert history.status == "ready" and history.frozen
    assert history.freeze_version == old.version
    assert history.input_versions == old.input_versions
    assert records.load(strategy, now.date()).payload_hash == formal.payload_hash


def test_authorized_production_freeze_keeps_existing_first_wins_contract(tmp_path: Path):
    now = NOW.replace(hour=15, minute=0)
    current = freeze()
    records = SQLiteFreezeRecords(tmp_path / "freeze.sqlite3")
    records.initialize()
    index = UnifiedFreezeIndex()
    assert index.publish(current, expected_version=None).accepted
    freezer = _freezer(index, records, _Clock(now), Strategy.TOMORROW, production_authority=True)

    first = freezer.freeze_scheduled()
    assert first.status == "frozen"
    assert freezer.freeze_scheduled().record == first.record
    assert records.load(Strategy.TOMORROW, now.date()) == first.record
