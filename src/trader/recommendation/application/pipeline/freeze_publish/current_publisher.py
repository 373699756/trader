"""Current snapshot and initial quote handoff, with actual publication receipts."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass

from trader.recommendation.application.pipeline.freeze_publish.publication_io import (
    ObservePublicationIo,
    PublicationIoTarget,
    PublicationIoTracker,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (
    UnifiedFreezeIndex,
    UnifiedFreezePublishResult,
    UnifiedPairedPublishResult,
    UnifiedProfilePair,
    UnifiedProfiledPublishResult,
)
from trader.recommendation.application.ports.runtime import FreezeBuilderPort
from trader.recommendation.application.runtime.trading_session import SessionGuard, SessionLease, StaleSessionError
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.publication.freeze_identity import FreezeIdentity, FreezeOverlay, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class PreparedCurrentPair:
    profile: str
    index: UnifiedFreezeIndex
    freezes: tuple[ScoredFreeze, ScoredFreeze]
    overlays: tuple[FreezeOverlay, FreezeOverlay]
    expected_versions: tuple[str | None, str | None]
    publication_io: PublicationIoTracker | None
    sessions: tuple[SessionLease | None, SessionLease | None]


def PrepareCurrentPair(
    profile: str,
    index: UnifiedFreezeIndex,
    builder: FreezeBuilderPort,
    freezes: tuple[ScoredFreeze, ScoredFreeze],
    *,
    expected_versions: tuple[str | None, str | None],
    publication_io: PublicationIoTracker | None,
    sessions: tuple[SessionLease | None, SessionLease | None],
) -> PreparedCurrentPair:
    if tuple(freeze.strategy for freeze in freezes) != (Strategy.TOMORROW, Strategy.D25):
        raise ValueError("paired publication requires tomorrow and d25")
    return PreparedCurrentPair(
        profile,
        index,
        freezes,
        (builder.initial_overlay(freezes[0]), builder.initial_overlay(freezes[1])),
        expected_versions,
        publication_io,
        sessions,
    )


def PublishCurrentBatch(
    pairs: tuple[PreparedCurrentPair, ...],
    publish_allowed: Callable[[], bool] = lambda: True,
) -> UnifiedProfiledPublishResult:
    with ExitStack() as stack:
        try:
            for pair in sorted(pairs, key=lambda item: item.profile):
                for session in pair.sessions:
                    stack.enter_context(SessionGuard(session))
        except StaleSessionError:
            return UnifiedProfiledPublishResult(False, "stale_session_generation")
        observations = tuple(
            (
                pair,
                tuple(
                    stack.enter_context(
                        ObservePublicationIo(pair.publication_io, "current_publish", PublicationIoTarget.freeze(item))
                    )
                    for item in pair.freezes
                ),
            )
            for pair in pairs
        )
        result = UnifiedFreezeIndex.publish_scored_batch(
            tuple(
                UnifiedProfilePair(
                    pair.profile, pair.index, pair.freezes, pair.overlays, pair.expected_versions
                )
                for pair in pairs
            ),
            publish_allowed,
        )
        for pair, profile_observations in observations:
            for observation, freeze in zip(profile_observations, pair.freezes, strict=True):
                observation.reason = result.reason
                observation.state = StageState.READY if result.accepted else StageState.FAILED
                observation.output_version = freeze.version if result.accepted else None
        return result


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
