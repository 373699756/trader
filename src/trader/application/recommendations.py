"""Recommendation generation use case from normalized feature snapshots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.board_scoring import BoardScoringCoordinator
from trader.application.board_scoring_cache import ScoringCacheContext
from trader.application.cache import request_fingerprint
from trader.application.policy import RecommendationPolicy
from trader.application.ports.reviews import DeepSeekReviewPort
from trader.application.recommendation_finalization import PreparedSnapshot, RecommendationFinalizationMixin
from trader.application.recommendation_replay import (
    RecommendationReplayMixin,
)
from trader.domain.market.models import (
    Board,
    FeatureSnapshot,
)
from trader.domain.recommendation.filters import FilterResult, board_for_snapshot, hard_filter
from trader.domain.recommendation.models import (
    BoardScoreBatch,
    FilterAudit,
    RecommendationSnapshot,
    Strategy,
)
from trader.domain.recommendation.ranking import (
    CORE_FIELDS,
    candidate_score,
)

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


class RecommendationEngine(RecommendationFinalizationMixin, RecommendationReplayMixin):
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

    def board_scoring_status(self) -> Mapping[str, Mapping[str, int | float | bool]]:
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
            discovery_snapshot = replace(
                snapshot,
                quote=replace(
                    snapshot.quote,
                    source_time=min(now, snapshot.quote.received_time),
                ),
            )
            result = self._hard_filter(
                discovery_snapshot,
                now,
                max_age_seconds=max_age_seconds,
                policy=self._policy.hard_filter,
            )
            if not result.allowed:
                reasons.update(reason.code for reason in result.reasons)
                if snapshot.history_days < 20 and any(
                    reason.code in {"missing_liquidity_history", "invalid_liquidity_history"}
                    for reason in result.reasons
                ):
                    reasons["history_warming"] += 1
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
        resolved_epoch = (
            merge_epoch or request_fingerprint({"data_version": resolved_data_version, "features": material})[:24]
        )
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
                    board_degraded_reasons.extend(f"{batch.board.value}:{reason}" for reason in batch.degraded_reasons)
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


__all__ = ["PreparedSnapshot", "RecommendationEngine"]
