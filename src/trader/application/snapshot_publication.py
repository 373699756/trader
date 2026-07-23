"""P6-first admission for runtime snapshot publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trader.domain.recommendation.models import RecommendationSnapshot

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline


def admit_snapshot_to_p6(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
) -> bool:
    """Admit a snapshot before mutating runtime/checkpoint/SSE publication state."""

    if pipeline._published_snapshots is pipeline._state:
        return True
    try:
        accepted = pipeline._published_snapshots.publish(snapshot)
    except ValueError as exc:
        _record_rejection(pipeline, snapshot, type(exc).__name__)
        return False
    if accepted is False:
        _record_rejection(pipeline, snapshot, "not_current")
        return False
    return True


def _record_rejection(
    pipeline: RecommendationPipeline,
    snapshot: RecommendationSnapshot,
    reason: str,
) -> None:
    pipeline._state.increment("p6_snapshot_rejections")
    pipeline._state.record_strategy_degraded(snapshot.strategy, ("p6_snapshot_rejected",))
    pipeline._state.record_error(f"{snapshot.strategy.value} snapshot retained previous P6 view: {reason}")


__all__ = ["admit_snapshot_to_p6"]
