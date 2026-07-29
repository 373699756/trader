"""Pure projections for recommendation data that is allowed to reach disk."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from trader.domain.recommendation.models import (
    BoardScoreBatch,
    RecommendationAction,
    RecommendationReplayInput,
    RecommendationSnapshot,
)
from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
)
from trader.domain.recommendation.tomorrow_fusion import DecisionEpoch
from trader.domain.review.models import ReviewOutcome

OFFICIAL_REPLAY_ALGORITHM_VERSION = "engine_review31_official_only_2026_07"
OFFICIAL_PERSISTENCE_SCOPE = "executable_only_v1"


def official_snapshot(snapshot: RecommendationSnapshot) -> RecommendationSnapshot:
    """Remove every non-executable stock identity before persistence."""

    if snapshot.strategy.value == "long":
        raise ValueError("long snapshots are never official recommendation records")
    recommendations = tuple(item for item in snapshot.recommendations if item.action is RecommendationAction.EXECUTABLE)
    codes = frozenset(item.features.quote.code for item in recommendations)
    diagnostics_raw = snapshot.metadata.get("selection_diagnostics")
    diagnostics = dict(diagnostics_raw) if isinstance(diagnostics_raw, Mapping) else {}
    maximum_local_score = max((item.score.local_score for item in recommendations), default=None)
    maximum_final_score = max((item.score.final_score for item in recommendations), default=None)
    diagnostics.update(
        {
            "scored_candidate_count": len(recommendations),
            "actionable_candidate_count": len(recommendations),
            "score_qualified_count": len(recommendations),
            "observation_floor": None,
            "observation_limit": 0,
            "selected_executable_count": len(recommendations),
            "selected_observation_count": 0,
            "blocked_reason_counts": {},
            "selection_skip_reason_counts": {},
            "maximum_local_score": maximum_local_score,
            "maximum_final_score": maximum_final_score,
            "empty_reason": None if recommendations else "no_formal_recommendations_at_freeze",
        }
    )
    metadata = _official_metadata(
        snapshot.metadata,
        codes,
        reviewed_count=sum(
            item.review is not None and item.review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
            for item in recommendations
        ),
    )
    return replace(
        snapshot,
        recommendations=recommendations,
        filter_details=tuple(item for item in snapshot.filter_details if item.stock_code in codes),
        degraded_reasons=tuple(
            reason for reason in snapshot.degraded_reasons if reason != "close_fallback_observe_floor"
        ),
        metadata={
            **metadata,
            "persistence_scope": OFFICIAL_PERSISTENCE_SCOPE,
            "selection_diagnostics": diagnostics,
        },
        replay_input=_official_replay_input(snapshot.replay_input, codes),
    )


def official_decision(decision: DecisionEpoch) -> DecisionEpoch:
    """Project a native tomorrow decision to formal executable entries."""

    entries = tuple(
        item for item in decision.entries if item.selected and item.action is RecommendationAction.EXECUTABLE
    )
    codes = frozenset(item.code for item in entries)
    return replace(
        decision,
        entries=entries,
        review_candidate_codes=tuple(code for code in decision.review_candidate_codes if code in codes),
        evaluated_count=len(entries),
        rejected_count=0,
        unscored_count=0,
        filter_reason_counts={},
    )


def official_checkpoint(checkpoint: TomorrowFreezeCheckpoint) -> TomorrowFreezeCheckpoint:
    """Project a native checkpoint to formal executable entries."""

    return replace(checkpoint, decision=official_decision(checkpoint.decision))


def official_freeze(frozen: TomorrowDecisionFreeze) -> TomorrowDecisionFreeze:
    """Project a native frozen decision and its anchors to formal entries."""

    decision = official_decision(frozen.decision)
    codes = frozenset(item.code for item in decision.entries)
    return replace(
        frozen,
        decision=decision,
        anchors=tuple(anchor for anchor in frozen.anchors if anchor.code in codes),
    )


def _official_replay_input(
    replay_input: RecommendationReplayInput | None,
    codes: frozenset[str],
) -> RecommendationReplayInput | None:
    if replay_input is None:
        return None
    candidate_features = tuple(item for item in replay_input.candidate_features if item.quote.code in codes)
    requested_codes = tuple(item.quote.code for item in candidate_features)
    return replace(
        replay_input,
        algorithm_version=OFFICIAL_REPLAY_ALGORITHM_VERSION,
        market_features=tuple(item for item in replay_input.market_features if item.quote.code in codes),
        requested_codes=requested_codes,
        candidate_features=candidate_features,
        reviews={code: review for code, review in replay_input.reviews.items() if code in codes},
        candidate_pool_size=len(candidate_features),
        target_prices={code: price for code, price in replay_input.target_prices.items() if code in codes},
        board_batches=tuple(_official_board_batch(batch, codes) for batch in replay_input.board_batches),
    )


def _official_board_batch(batch: BoardScoreBatch, codes: frozenset[str]) -> BoardScoreBatch:
    return replace(
        batch,
        recommendations=tuple(item for item in batch.recommendations if item.features.quote.code in codes),
    )


def _official_metadata(
    metadata: Mapping[str, object],
    codes: frozenset[str],
    *,
    reviewed_count: int,
) -> dict[str, object]:
    projected = dict(metadata)
    projected.pop("close_fallback_observe_floor", None)
    for key in (
        "corporate_risk_covered_count",
        "corporate_risk_coverage_ratio",
        "corporate_risk_registry_versions",
        "research_data_covered_count",
        "research_data_coverage_ratio",
        "research_data_required_fields",
        "tail_data_covered_count",
        "tail_data_coverage_ratio",
    ):
        projected.pop(key, None)
    projected["candidate_count"] = len(codes)
    projected["reviewed_count"] = reviewed_count
    projected["selection_skips"] = []
    for key in ("close_anchors", "field_sources", "freeze_anchor"):
        raw = projected.get(key)
        if isinstance(raw, Mapping):
            projected[key] = {str(code): value for code, value in raw.items() if str(code) in codes}
    missing = projected.get("market_missing_reasons")
    if isinstance(missing, Mapping):
        projected["market_missing_reasons"] = {
            str(key): value for key, value in missing.items() if str(key).partition(".")[0] in codes
        }
    conflicts = projected.get("market_conflicts")
    if isinstance(conflicts, (list, tuple)):
        projected["market_conflicts"] = tuple(
            str(conflict) for conflict in conflicts if str(conflict).rpartition(":")[2] in codes
        )
    return projected


__all__ = [
    "OFFICIAL_PERSISTENCE_SCOPE",
    "OFFICIAL_REPLAY_ALGORITHM_VERSION",
    "official_decision",
    "official_checkpoint",
    "official_freeze",
    "official_snapshot",
]
