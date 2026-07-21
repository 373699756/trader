"""Recommendation generation use case from normalized feature snapshots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType

from trader.application.board_scoring_cache import ScoringCacheContext
from trader.application.cache import request_fingerprint
from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.policy import RecommendationPolicy
from trader.application.ports import DeepSeekReviewPort
from trader.application.recommendation_replay import (
    REPLAY_ALGORITHM_VERSION,
    REPLAY_SCHEMA_VERSION,
    RecommendationReplayMixin,
)
from trader.application.recommendation_support import (
    _freeze_policy,
    _fusion_mode,
    _preselection_replay_feature,
    _review_contexts_for_candidates,
    _snapshot_id,
)
from trader.domain.filters import FilterResult, board_for_snapshot, hard_filter
from trader.domain.fusion import fuse_score
from trader.domain.models import (
    Board,
    BoardScoreBatch,
    BoardStrategyPolicy,
    DeepSeekReview,
    FeatureSnapshot,
    FilterAudit,
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationReplayInput,
    RecommendationSnapshot,
    ReviewCandidateContext,
    ReviewOutcome,
    Strategy,
)
from trader.domain.ranking import (
    CORE_FIELDS,
    action_for,
    candidate_score,
    minimum_selection_score,
    select_top_k_with_audit,
)
from trader.domain.risk import derive_local_risk_facts
from trader.domain.strategies import score_strategy
from trader.domain.strategies.composition import LocalScoreResult, compose
from trader.domain.tail import TAIL_SIGNAL_VALUE_FIELDS

_PRESELECTION_VALUE_FIELDS = (*CORE_FIELDS, "amount_median_20d", "trend_score")
_STRUCTURED_RISK_FIELDS = (
    "financial_deterioration",
    "negative_announcement_level",
    "pledge_risk",
    "shareholder_reduction_level",
    "unlock_risk",
)
_LONG_RESEARCH_FIELDS = (
    "value_score",
    "growth_score",
    "quality_score",
    "industry_policy_score",
    "risk_protection_score",
    *_STRUCTURED_RISK_FIELDS,
)


def _merge_epoch_for_features(features: Sequence[FeatureSnapshot], data_version: str) -> str:
    """Return one deterministic epoch for a feature batch.

    Market adapters may already bind a canonical merge epoch.  When they do
    not, the input versions and codes are hashed so all three board lanes see
    the same immutable identity without consulting an external clock or store.
    """

    epochs = {feature.merge_epoch for feature in features if feature.merge_epoch}
    if len(epochs) == 1:
        return next(iter(epochs))
    material = tuple(
        (feature.quote.code, feature.quote.data_version, feature.merge_epoch)
        for feature in sorted(features, key=lambda item: item.quote.code)
    )
    return request_fingerprint({"data_version": data_version, "features": material})[:24]


@dataclass(frozen=True)
class PreparedSnapshot:
    strategy: Strategy
    features: tuple[FeatureSnapshot, ...]
    eligible: tuple[FeatureSnapshot, ...]
    local_candidates: tuple[Recommendation, ...]
    now: datetime
    phase: str
    trade_date: str
    data_version: str
    review_deadline: datetime
    max_age_seconds: float
    filtered_count: int
    filter_reasons: Mapping[str, int]
    filter_details: tuple[FilterAudit, ...]
    target_prices: Mapping[str, float | None]
    market_features: tuple[FeatureSnapshot, ...]
    requested_codes: tuple[str, ...]
    preselect_max_age_seconds: float
    candidate_pool_size: int
    board_batches: tuple[BoardScoreBatch, ...] = ()
    board_scoring_complete: bool = True
    board_degraded_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "filter_reasons", MappingProxyType(dict(self.filter_reasons)))
        object.__setattr__(self, "target_prices", MappingProxyType(dict(self.target_prices)))

    @property
    def review_eligible(self) -> tuple[FeatureSnapshot, ...]:
        if not self.board_scoring_complete:
            return ()
        return tuple(
            feature
            for feature in self.eligible
            if self.strategy is Strategy.LONG or feature.board_data_reliability >= 0.85
        )


class RecommendationEngine(RecommendationReplayMixin):
    def __init__(
        self,
        policy: RecommendationPolicy,
        *,
        hard_filter_function: Callable[..., FilterResult] = hard_filter,
        board_scoring: BoardScoringCoordinator | None = None,
    ) -> None:
        self._policy = policy
        self._hard_filter = hard_filter_function
        self._board_scoring = board_scoring or BoardScoringCoordinator()

    def start(self) -> None:
        self._board_scoring.start()

    def stop(self) -> None:
        self._board_scoring.stop()

    def board_scoring_status(self) -> Mapping[str, Mapping[str, int | bool]]:
        return self._board_scoring.status()

    def preselect(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        max_age_seconds: float,
        limit: int,
        strategies: Sequence[Strategy] | None = None,
        trade_date: str | None = None,
        phase: str = "preselection",
        data_version: str | None = None,
        merge_epoch: str | None = None,
    ) -> tuple[tuple[FeatureSnapshot, ...], Mapping[str, int], tuple[FilterAudit, ...]]:
        accepted: list[FeatureSnapshot] = []
        reasons: Counter[str] = Counter()
        details: list[FilterAudit] = []
        for snapshot in features:
            result = self._hard_filter(
                snapshot,
                now,
                max_age_seconds=max_age_seconds,
                policy=self._policy.hard_filter,
            )
            if not result.allowed:
                reasons.update(reason.code for reason in result.reasons)
                details.extend(result.reasons)
                continue
            board = board_for_snapshot(snapshot)
            accepted.append(replace(snapshot, quote=replace(snapshot.quote, board=board)))
        if not self._policy.board_candidate_weights:
            legacy_accepted: list[tuple[float, FeatureSnapshot]] = []
            for snapshot in accepted:
                if snapshot.missing_ratio(CORE_FIELDS) > 0.30:
                    reasons["insufficient_candidate_history"] += 1
                    details.append(
                        FilterAudit(
                            stock_code=snapshot.quote.code,
                            filter_code="insufficient_candidate_history",
                            threshold="<= 0.30",
                            actual=round(snapshot.missing_ratio(CORE_FIELDS), 6),
                            source=snapshot.quote.source,
                            observed_at=snapshot.quote.source_time,
                        )
                    )
                    continue
                legacy_accepted.append((candidate_score(snapshot, self._policy.candidate_weights), snapshot))
            legacy_accepted.sort(key=lambda item: (-item[0], item[1].quote.code))
            return tuple(snapshot for _score, snapshot in legacy_accepted[:limit]), dict(reasons), tuple(details)

        active = tuple(strategies or (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25))
        market_by_code = {snapshot.quote.code: snapshot for snapshot in accepted}
        context = self._scoring_context(
            tuple(accepted),
            now=now,
            trade_date=trade_date,
            phase=phase,
            data_version=data_version,
            merge_epoch=merge_epoch,
        )
        selected: dict[str, tuple[float, FeatureSnapshot]] = {}
        board_limit = min(max(0, limit), 120)
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR):
            board_features = tuple(item for item in market_by_code.values() if item.quote.board is board)
            for strategy in active:
                if strategy is Strategy.LONG:
                    continue
                policy = self._policy.board_policy(strategy, board)
                if policy is None:
                    continue
                candidates = self._board_scoring.preselect(
                    strategy,
                    board_features,
                    policy,
                    context,
                    limit=board_limit,
                )
                for feature in candidates:
                    score_raw = feature.optional_value("board_candidate_score")
                    score = score_raw if score_raw is not None else 0.0
                    previous = selected.get(feature.quote.code)
                    if previous is None or score > previous[0]:
                        selected[feature.quote.code] = (score, feature)
        return (
            tuple(item[1] for item in sorted(selected.values(), key=lambda item: (-item[0], item[1].quote.code))),
            dict(reasons),
            tuple(details),
        )

    def _scoring_context(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        trade_date: str | None,
        phase: str,
        data_version: str | None,
        merge_epoch: str | None,
    ) -> ScoringCacheContext:
        material = tuple(
            (
                feature.quote.code,
                feature.quote.data_version,
                feature.merge_epoch,
            )
            for feature in sorted(features, key=lambda item: item.quote.code)
        )
        resolved_data_version = data_version or request_fingerprint({"features": material})[:24]
        resolved_epoch = merge_epoch or request_fingerprint(
            {"data_version": resolved_data_version, "features": material}
        )[:24]
        return ScoringCacheContext(
            trade_date=trade_date or now.date().isoformat(),
            phase=phase,
            merge_epoch=resolved_epoch,
            data_version=resolved_data_version,
            observed_at=now,
        )

    def build_snapshot(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        phase: str,
        trade_date: str,
        data_version: str,
        review_port: DeepSeekReviewPort | None,
        review_deadline: datetime,
        max_age_seconds: float,
        filtered_count: int,
        filter_reasons: Mapping[str, int],
        filter_details: Sequence[FilterAudit] = (),
        target_prices: Mapping[str, float | None] | None = None,
        market_features: Sequence[FeatureSnapshot] = (),
        requested_codes: Sequence[str] = (),
        preselect_max_age_seconds: float | None = None,
        candidate_pool_size: int = 0,
    ) -> RecommendationSnapshot:
        prepared = self.prepare_snapshot(
            strategy,
            features,
            now=now,
            phase=phase,
            trade_date=trade_date,
            data_version=data_version,
            review_deadline=review_deadline,
            max_age_seconds=max_age_seconds,
            filtered_count=filtered_count,
            filter_reasons=filter_reasons,
            filter_details=filter_details,
            target_prices=target_prices,
            market_features=market_features,
            requested_codes=requested_codes,
            preselect_max_age_seconds=preselect_max_age_seconds,
            candidate_pool_size=candidate_pool_size,
        )
        reviews = (
            review_port.review(
                strategy,
                prepared.review_eligible,
                phase=phase,
                deadline=review_deadline,
                contexts=self.review_contexts(prepared),
            )
            if review_port is not None and prepared.review_eligible
            else {}
        )
        return self.finalize_snapshot(prepared, reviews)

    def prepare_snapshot(
        self,
        strategy: Strategy,
        features: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        phase: str,
        trade_date: str,
        data_version: str,
        review_deadline: datetime,
        max_age_seconds: float,
        filtered_count: int,
        filter_reasons: Mapping[str, int],
        filter_details: Sequence[FilterAudit] = (),
        target_prices: Mapping[str, float | None] | None = None,
        market_features: Sequence[FeatureSnapshot] = (),
        requested_codes: Sequence[str] = (),
        preselect_max_age_seconds: float | None = None,
        candidate_pool_size: int = 0,
    ) -> PreparedSnapshot:
        eligible: list[FeatureSnapshot] = []
        refreshed_filter_reasons = Counter(filter_reasons)
        refreshed_filter_details = list(filter_details)
        refreshed_filtered_count = filtered_count
        for feature in features:
            filter_result = self._hard_filter(
                feature,
                now,
                max_age_seconds=max_age_seconds,
                policy=self._policy.hard_filter,
            )
            if filter_result.allowed:
                refreshed_filter_details.extend(filter_result.optional_flags)
                eligible.append(feature)
                continue
            refreshed_filter_reasons.update(reason.code for reason in filter_result.reasons)
            refreshed_filter_details.extend(filter_result.reasons)
            refreshed_filtered_count += 1

        normalized_eligible = tuple(
            replace(feature, quote=replace(feature.quote, board=board_for_snapshot(feature))) for feature in eligible
        )
        board_batches: tuple[BoardScoreBatch, ...] = ()
        board_scoring_complete = True
        board_degraded_reasons: list[str] = []
        if strategy is not Strategy.LONG and self._policy.board_candidate_weights:
            policies = {
                board: policy
                for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
                if (policy := self._policy.board_policy(strategy, board)) is not None
            }
            if len(policies) != 3:
                raise RuntimeError(f"v16 board policies are incomplete for {strategy.value}")
            context = self._scoring_context(
                normalized_eligible,
                now=now,
                trade_date=trade_date,
                phase=phase,
                data_version=data_version,
                merge_epoch=_merge_epoch_for_features(normalized_eligible, data_version),
            )
            board_batches = self._board_scoring.score(
                strategy,
                normalized_eligible,
                policies,
                context,
                lambda scored_strategy, feature, policy, local_score: self._local_candidate_with_policy(
                    scored_strategy,
                    feature,
                    now,
                    policy,
                    local_score,
                ),
            )
            expected_epoch = context.merge_epoch
            if len(board_batches) != 3:
                board_scoring_complete = False
                board_degraded_reasons.append("board_batch_count_mismatch")
            for batch in board_batches:
                if batch.merge_epoch != expected_epoch:
                    board_scoring_complete = False
                    board_degraded_reasons.append(f"{batch.board.value}:merge_epoch_mismatch")
                if batch.status == "failed":
                    board_scoring_complete = False
                    board_degraded_reasons.extend(
                        f"{batch.board.value}:{reason}" for reason in batch.degraded_reasons or ("failed",)
                    )
                elif batch.status in {"degraded", "empty"}:
                    board_degraded_reasons.extend(
                        f"{batch.board.value}:{reason}" for reason in batch.degraded_reasons
                    )
            local_candidates = tuple(item for batch in board_batches for item in batch.recommendations)
            if board_scoring_complete:
                normalized_eligible = tuple(item.features for item in local_candidates)
        else:
            local_candidates = tuple(self._local_candidate(strategy, feature, now) for feature in normalized_eligible)

        return PreparedSnapshot(
            strategy=strategy,
            features=tuple(features),
            eligible=normalized_eligible,
            local_candidates=local_candidates,
            now=now,
            phase=phase,
            trade_date=trade_date,
            data_version=data_version,
            review_deadline=review_deadline,
            max_age_seconds=max_age_seconds,
            filtered_count=refreshed_filtered_count,
            filter_reasons=dict(refreshed_filter_reasons),
            filter_details=tuple(refreshed_filter_details),
            target_prices=dict(target_prices or {}),
            market_features=tuple(market_features),
            requested_codes=tuple(requested_codes),
            preselect_max_age_seconds=preselect_max_age_seconds
            if preselect_max_age_seconds is not None
            else max_age_seconds,
            candidate_pool_size=candidate_pool_size,
            board_batches=board_batches,
            board_scoring_complete=board_scoring_complete,
            board_degraded_reasons=tuple(dict.fromkeys(board_degraded_reasons)),
        )

    def finalize_snapshot(
        self,
        prepared: PreparedSnapshot,
        reviews: Mapping[str, DeepSeekReview],
    ) -> RecommendationSnapshot:
        if not prepared.board_scoring_complete:
            reasons = ",".join(prepared.board_degraded_reasons) or "unknown"
            raise RuntimeError(f"v16 board scoring is incomplete: {reasons}")
        strategy = prepared.strategy
        eligible = prepared.eligible
        now = prepared.now
        phase = prepared.phase
        merged, fusion_mode = self._merge_reviewed_candidates(
            strategy,
            prepared.local_candidates,
            reviews,
            now=now,
            phase=phase,
            max_age_seconds=prepared.max_age_seconds,
            target_prices=prepared.target_prices,
        )
        minimum_score = minimum_selection_score(
            strategy,
            self._policy.selection.thresholds,
            phase=phase,
            observation_margin=self._policy.selection.observation_margin,
        )
        selected, selection_skips = (
            select_top_k_with_audit(
                merged,
                top_k=self._policy.selection.default_top_k,
                maximum_per_industry=self._policy.selection.maximum_per_industry,
                minimum_final_score=minimum_score,
                maximum_board_fraction=self._policy.selection.maximum_board_fraction,
                competition_group_limits=self._policy.selection.competition_group_limits,
            )
            if minimum_score is not None
            else ((), ())
        )
        snapshot_id = _snapshot_id(strategy, prepared.trade_date, phase, prepared.data_version, now)
        degraded_reasons: list[str] = list(prepared.board_degraded_reasons)
        if fusion_mode is FusionMode.LOCAL_DEGRADED:
            degraded_reasons.append("deepseek_incomplete")
        tail_covered_count = 0
        if strategy is Strategy.TOMORROW:
            tail_covered_count = sum(
                all(feature.optional_value(field) is not None for field in TAIL_SIGNAL_VALUE_FIELDS)
                for feature in eligible
            )
            if tail_covered_count != len(eligible):
                degraded_reasons.append("tomorrow_tail_data_incomplete")
        research_covered_count = 0
        research_fields: tuple[str, ...] = ()
        if strategy in {Strategy.D25, Strategy.LONG}:
            research_fields = _LONG_RESEARCH_FIELDS if strategy is Strategy.LONG else _STRUCTURED_RISK_FIELDS
            research_covered_count = sum(
                all(feature.optional_value(field) is not None for field in research_fields) for feature in eligible
            )
            if research_covered_count != len(eligible):
                degraded_reasons.append(
                    "long_research_incomplete" if strategy is Strategy.LONG else "d25_structured_research_incomplete"
                )
        return RecommendationSnapshot(
            snapshot_id=snapshot_id,
            strategy=strategy,
            trade_date=prepared.trade_date,
            phase=phase,
            data_version=prepared.data_version,
            strategy_version=self._policy.strategy_version,
            fusion_version=self._policy.fusion_version,
            fusion_mode=fusion_mode,
            published_at=now,
            recommendations=selected,
            filtered_count=prepared.filtered_count,
            filter_reasons=dict(prepared.filter_reasons),
            filter_details=prepared.filter_details,
            stale=any(item.features.quote.age_seconds(now) > prepared.max_age_seconds for item in selected),
            degraded_reasons=tuple(degraded_reasons),
            metadata={
                "candidate_count": len(eligible),
                "reviewed_count": sum(
                    review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN} for review in reviews.values()
                ),
                "board_batches": tuple(
                    {
                        "board": batch.board.value,
                        "status": batch.status,
                        "policy_id": batch.policy_id,
                        "policy_version": batch.policy_version,
                        "merge_epoch": batch.merge_epoch,
                        "population_version": batch.population_version,
                        "degraded_reasons": batch.degraded_reasons,
                    }
                    for batch in prepared.board_batches
                ),
                "selection_skips": tuple(
                    {
                        "stock_code": skip.stock_code,
                        "board": skip.board.value,
                        "competition_group_id": skip.competition_group_id,
                        "board_rank": skip.board_rank,
                        "global_rank": skip.global_rank,
                        "reason": skip.reason,
                        "limit": skip.limit,
                        "policy_version": skip.policy_version,
                        "observed_at": skip.observed_at.isoformat(),
                    }
                    for skip in selection_skips
                ),
                **(
                    {
                        "tail_data_covered_count": tail_covered_count,
                        "tail_data_coverage_ratio": tail_covered_count / len(eligible) if eligible else 0.0,
                    }
                    if strategy is Strategy.TOMORROW
                    else {}
                ),
                **(
                    {
                        "research_data_covered_count": research_covered_count,
                        "research_data_coverage_ratio": research_covered_count / len(eligible) if eligible else 0.0,
                        "research_data_required_fields": research_fields,
                    }
                    if research_fields
                    else {}
                ),
            },
            replay_input=RecommendationReplayInput(
                schema_version=REPLAY_SCHEMA_VERSION,
                algorithm_version=REPLAY_ALGORITHM_VERSION,
                policy=_freeze_policy(self._policy),
                evaluated_at=now,
                market_features=tuple(_preselection_replay_feature(feature) for feature in prepared.market_features),
                requested_codes=prepared.requested_codes or tuple(feature.quote.code for feature in prepared.features),
                candidate_features=prepared.features,
                reviews=dict(reviews),
                preselect_max_age_seconds=prepared.preselect_max_age_seconds,
                score_max_age_seconds=prepared.max_age_seconds,
                candidate_pool_size=prepared.candidate_pool_size,
                target_prices=dict(prepared.target_prices),
                board_batches=prepared.board_batches,
            ),
        )

    def prepare_frozen_board_replay(
        self,
        strategy: Strategy,
        replay_input: RecommendationReplayInput,
        *,
        phase: str,
        trade_date: str,
        data_version: str,
        filtered_count: int,
        filter_reasons: Mapping[str, int],
        filter_details: Sequence[FilterAudit],
    ) -> PreparedSnapshot:
        batches = replay_input.board_batches
        if len(batches) != 3 or {batch.board for batch in batches} != {Board.MAIN, Board.CHINEXT, Board.STAR}:
            raise ValueError("v16 frozen replay requires exactly three board batches")
        epochs = {batch.merge_epoch for batch in batches}
        if len(epochs) != 1 or any(batch.strategy is not strategy or batch.status == "failed" for batch in batches):
            raise ValueError("v16 frozen board batches are incomplete or have mixed epochs")
        for batch in batches:
            policy = self._policy.board_policy(strategy, batch.board)
            if policy is None or batch.policy_id != policy.policy_id:
                raise ValueError("v16 frozen board batch policy does not match replay policy")
        candidates = tuple(item for batch in batches for item in batch.recommendations)
        degraded = tuple(
            dict.fromkeys(
                f"{batch.board.value}:{reason}"
                for batch in batches
                for reason in batch.degraded_reasons
            )
        )
        return PreparedSnapshot(
            strategy=strategy,
            features=replay_input.candidate_features,
            eligible=tuple(item.features for item in candidates),
            local_candidates=candidates,
            now=replay_input.evaluated_at,
            phase=phase,
            trade_date=trade_date,
            data_version=data_version,
            review_deadline=replay_input.evaluated_at,
            max_age_seconds=replay_input.score_max_age_seconds,
            filtered_count=filtered_count,
            filter_reasons=filter_reasons,
            filter_details=tuple(filter_details),
            target_prices=replay_input.target_prices,
            market_features=replay_input.market_features,
            requested_codes=replay_input.requested_codes,
            preselect_max_age_seconds=replay_input.preselect_max_age_seconds,
            candidate_pool_size=replay_input.candidate_pool_size,
            board_batches=batches,
            board_degraded_reasons=degraded,
        )

    def review_contexts(self, prepared: PreparedSnapshot) -> Mapping[str, ReviewCandidateContext]:
        review_codes = {feature.quote.code for feature in prepared.review_eligible}
        return _review_contexts_for_candidates(
            prepared.strategy,
            tuple(
                candidate
                for candidate in prepared.local_candidates
                if candidate.features.quote.code in review_codes
            ),
            prepared.phase,
            self._policy.selection,
        )

    def _merge_candidates(
        self,
        strategy: Strategy,
        eligible: Sequence[FeatureSnapshot],
        *,
        now: datetime,
        phase: str,
        review_port: DeepSeekReviewPort | None,
        review_deadline: datetime,
        max_age_seconds: float,
        target_prices: Mapping[str, float | None] | None = None,
    ) -> tuple[tuple[Recommendation, ...], Mapping[str, DeepSeekReview], FusionMode]:
        local_candidates = tuple(self._local_candidate(strategy, feature, now) for feature in eligible)
        reviews = (
            review_port.review(
                strategy,
                tuple(eligible),
                phase=phase,
                deadline=review_deadline,
                contexts=_review_contexts_for_candidates(
                    strategy,
                    local_candidates,
                    phase,
                    self._policy.selection,
                ),
            )
            if review_port is not None and eligible
            else {}
        )
        merged, fusion_mode = self._merge_reviewed_candidates(
            strategy,
            local_candidates,
            reviews,
            now=now,
            phase=phase,
            max_age_seconds=max_age_seconds,
            target_prices=target_prices,
        )
        return merged, reviews, fusion_mode

    def _merge_reviewed_candidates(
        self,
        strategy: Strategy,
        local_candidates: Sequence[Recommendation],
        reviews: Mapping[str, DeepSeekReview],
        *,
        now: datetime,
        phase: str,
        max_age_seconds: float,
        target_prices: Mapping[str, float | None] | None = None,
    ) -> tuple[tuple[Recommendation, ...], FusionMode]:
        fusion_candidates = tuple(
            candidate
            for candidate in local_candidates
            if strategy is Strategy.LONG
            or candidate.features.board_data_reliability >= self._policy.selection.minimum_board_reliability
        )
        fusion_mode = _fusion_mode(fusion_candidates, reviews, self._policy.selection.thresholds, strategy, phase)
        merged: list[Recommendation] = []
        for local in local_candidates:
            review = reviews.get(local.features.quote.code)
            board_policy = self._policy.board_policy(strategy, local.features.quote.board)
            if local.features.board_policy_id != (board_policy.policy_id if board_policy is not None else ""):
                board_policy = None
            local_score = (
                compose(local.score.components, board_policy.local_weights)
                if board_policy is not None
                else score_strategy(strategy, local.features, self._policy.local_strategy_weights)
            )
            fusion_result = fuse_score(
                local_score,
                local.local_risk_facts,
                review,
                self._policy.dimension_weights[strategy],
                self._policy.risk_rules,
                fusion_mode,
                self._policy.fusion,
                evidence=local.features.evidence,
                evaluated_at=now,
            )
            provisional = replace(
                local,
                score=fusion_result.score,
                deepseek_risk_facts=fusion_result.deepseek_risk_facts,
                review=review,
                veto=fusion_result.veto,
                target_price=(target_prices or {}).get(local.features.quote.code),
            )
            action, reason = action_for(
                provisional,
                self._policy.selection.thresholds,
                phase=phase,
                is_stale=local.features.quote.age_seconds(now) > max_age_seconds,
                observation_margin=self._policy.selection.observation_margin,
                minimum_board_reliability=self._policy.selection.minimum_board_reliability,
            )
            restrictions = local.features.quote.execution_restrictions
            if restrictions and action is RecommendationAction.EXECUTABLE:
                action = RecommendationAction.OBSERVE
                reason = "market_data_observe_only:" + ",".join(sorted(restrictions))
            merged.append(replace(provisional, action=action, action_reason=reason))
        return tuple(merged), fusion_mode

    def _local_candidate(
        self,
        strategy: Strategy,
        features: FeatureSnapshot,
        now: datetime,
    ) -> Recommendation:
        local_facts = derive_local_risk_facts(features, now, self._policy.risk_rules, strategy=strategy)
        local = score_strategy(strategy, features, self._policy.local_strategy_weights)
        local_result = fuse_score(
            local,
            local_facts,
            None,
            self._policy.dimension_weights[strategy],
            self._policy.risk_rules,
            FusionMode.LOCAL_DEGRADED,
            self._policy.fusion,
        )
        return Recommendation(
            strategy=strategy,
            features=features,
            score=local_result.score,
            local_risk_facts=local_facts,
            deepseek_risk_facts=(),
            review=None,
            action=RecommendationAction.OBSERVE,
            action_reason="pending_merge",
            veto=False,
        )

    def _local_candidate_with_policy(
        self,
        strategy: Strategy,
        features: FeatureSnapshot,
        now: datetime,
        board_policy: BoardStrategyPolicy,
        local: LocalScoreResult,
    ) -> Recommendation:
        local_facts = derive_local_risk_facts(features, now, self._policy.risk_rules, strategy=strategy)
        local_result = fuse_score(
            local,
            local_facts,
            None,
            self._policy.dimension_weights[strategy],
            self._policy.risk_rules,
            FusionMode.LOCAL_DEGRADED,
            self._policy.fusion,
        )
        return Recommendation(
            strategy=strategy,
            features=features,
            score=local_result.score,
            local_risk_facts=local_facts,
            deepseek_risk_facts=(),
            review=None,
            action=RecommendationAction.OBSERVE,
            action_reason="pending_merge",
            veto=False,
        )


__all__ = ["PreparedSnapshot", "RecommendationEngine"]
