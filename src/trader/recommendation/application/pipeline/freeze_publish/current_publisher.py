"""Current snapshot and initial quote handoff, with actual publication receipts."""

from __future__ import annotations

from contextlib import ExitStack

from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    ObservePublicationIo,
    PublicationIoTarget,
    PublicationIoTracker,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedDecisionIndex,
    UnifiedDecisionPublishResult,
    UnifiedPairedPublishResult,
)
from trader.recommendation.application.ports.runtime import DecisionBuilderPort
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.decision_identity import DecisionIdentity, ScoredDecision
from trader.recommendation.domain.publication.models import Strategy


def PublishCurrentSnapshot(
    index: UnifiedDecisionIndex,
    builder: DecisionBuilderPort,
    identity: DecisionIdentity,
    *,
    expected_version: str | None,
    publication_io: PublicationIoTracker | None,
) -> UnifiedDecisionPublishResult:
    if not isinstance(identity, ScoredDecision):
        return index.publish(identity, expected_version=expected_version)
    with ObservePublicationIo(publication_io, "current_publish", PublicationIoTarget.decision(identity)) as observation:
        overlay = builder.initial_overlay(identity)
        result = index.publish_scored(identity, overlay, expected_version=expected_version)
        observation.reason = result.reason
        observation.state = StageState.READY if result.accepted else StageState.FAILED
        observation.output_version = identity.version if result.accepted else None
        return result


def PublishCurrentPair(
    index: UnifiedDecisionIndex,
    builder: DecisionBuilderPort,
    decisions: tuple[ScoredDecision, ScoredDecision],
    *,
    expected_versions: tuple[str | None, str | None],
    publication_io: PublicationIoTracker | None,
) -> UnifiedPairedPublishResult:
    if tuple(decision.strategy for decision in decisions) != (Strategy.TOMORROW, Strategy.D25):
        raise ValueError("paired publication requires tomorrow and d25")
    with ExitStack() as stack:
        observations = tuple(
            stack.enter_context(
                ObservePublicationIo(publication_io, "current_publish", PublicationIoTarget.decision(item))
            )
            for item in decisions
        )
        overlays = (builder.initial_overlay(decisions[0]), builder.initial_overlay(decisions[1]))
        result = index.publish_scored_pair(decisions, overlays, expected_versions)
        for observation, decision in zip(observations, decisions, strict=True):
            observation.reason = result.reason
            observation.state = StageState.READY if result.accepted else StageState.FAILED
            observation.output_version = decision.version if result.accepted else None
        return result
