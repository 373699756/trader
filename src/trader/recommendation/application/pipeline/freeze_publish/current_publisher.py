"""Current snapshot and initial quote handoff, with actual publication receipts."""

from __future__ import annotations

from contextlib import ExitStack

from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    ObservePublicationIo,
    PublicationIoTarget,
    PublicationIoTracker,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedFreezeIndex,
    UnifiedFreezePublishResult,
    UnifiedPairedPublishResult,
)
from trader.recommendation.application.ports.runtime import FreezeBuilderPort
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.freeze_identity import FreezeIdentity, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy


def PublishCurrentSnapshot(
    index: UnifiedFreezeIndex,
    builder: FreezeBuilderPort,
    identity: FreezeIdentity,
    *,
    expected_version: str | None,
    publication_io: PublicationIoTracker | None,
) -> UnifiedFreezePublishResult:
    if not isinstance(identity, ScoredFreeze):
        return index.publish(identity, expected_version=expected_version)
    with ObservePublicationIo(publication_io, "current_publish", PublicationIoTarget.freeze(identity)) as observation:
        overlay = builder.initial_overlay(identity)
        result = index.publish_scored(identity, overlay, expected_version=expected_version)
        observation.reason = result.reason
        observation.state = StageState.READY if result.accepted else StageState.FAILED
        observation.output_version = identity.version if result.accepted else None
        return result


def PublishCurrentPair(
    index: UnifiedFreezeIndex,
    builder: FreezeBuilderPort,
    freezes: tuple[ScoredFreeze, ScoredFreeze],
    *,
    expected_versions: tuple[str | None, str | None],
    publication_io: PublicationIoTracker | None,
) -> UnifiedPairedPublishResult:
    if tuple(freeze.strategy for freeze in freezes) != (Strategy.TOMORROW, Strategy.D25):
        raise ValueError("paired publication requires tomorrow and d25")
    with ExitStack() as stack:
        observations = tuple(
            stack.enter_context(
                ObservePublicationIo(publication_io, "current_publish", PublicationIoTarget.freeze(item))
            )
            for item in freezes
        )
        overlays = (builder.initial_overlay(freezes[0]), builder.initial_overlay(freezes[1]))
        result = index.publish_scored_pair(freezes, overlays, expected_versions)
        for observation, freeze in zip(observations, freezes, strict=True):
            observation.reason = result.reason
            observation.state = StageState.READY if result.accepted else StageState.FAILED
            observation.output_version = freeze.version if result.accepted else None
        return result
