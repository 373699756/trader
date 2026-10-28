"""Typed publication boundary for current recommendation identities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from trader.recommendation.domain.publication.freeze_identity import FreezeIdentity, FreezeOverlay, ScoredFreeze

OverlayPublisher = Callable[[FreezeOverlay], object]


class PublishResultPort(Protocol):
    @property
    def accepted(self) -> bool: ...

    @property
    def reason(self) -> str: ...


class FreezePublisherPort(Protocol):
    def publish(
        self,
        identity: FreezeIdentity,
        *,
        expected_version: str | None,
    ) -> PublishResultPort: ...

    def publish_scored(
        self,
        freeze: ScoredFreeze,
        initial_overlay: FreezeOverlay,
        *,
        expected_version: str | None,
    ) -> PublishResultPort: ...

    def publish_overlay(
        self,
        overlay: FreezeOverlay,
        *,
        expected_version: str | None,
    ) -> PublishResultPort: ...


__all__ = ["FreezePublisherPort", "OverlayPublisher", "PublishResultPort"]
