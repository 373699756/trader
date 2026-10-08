"""Current snapshot and initial quote handoff, with actual publication receipts."""

from __future__ import annotations

from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    PublicationIoTarget,
    PublicationIoTracker,
    observe_publication_io,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedDecisionIndex,
    UnifiedDecisionPublishResult,
)
from trader.recommendation.application.ports.runtime import DecisionBuilderPort
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.decision_identity import DecisionIdentity, ScoredDecision


def publish_current_snapshot(
    index: UnifiedDecisionIndex,
    builder: DecisionBuilderPort,
    identity: DecisionIdentity,
    *,
    expected_version: str | None,
    publication_io: PublicationIoTracker | None,
) -> UnifiedDecisionPublishResult:
    if not isinstance(identity, ScoredDecision):
        return index.publish(identity, expected_version=expected_version)
    with observe_publication_io(
        publication_io, "current_publish", PublicationIoTarget.decision(identity)
    ) as observation:
        overlay = builder.initial_overlay(identity)
        result = index.publish_scored(identity, overlay, expected_version=expected_version)
        observation.reason = result.reason
        observation.state = StageState.READY if result.accepted else StageState.FAILED
        observation.output_version = identity.version if result.accepted else None
        return result
