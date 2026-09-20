"""Typed publication boundary for current recommendation identities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from trader.recommendation.domain.publication.decision_identity import DecisionIdentity, DecisionOverlay, ScoredDecision

OverlayPublisher = Callable[[DecisionOverlay], object]


class PublishResultPort(Protocol):
    @property
    def accepted(self) -> bool: ...

    @property
    def reason(self) -> str: ...


class DecisionPublisherPort(Protocol):
    def publish(
        self,
        identity: DecisionIdentity,
        *,
        expected_version: str | None,
    ) -> PublishResultPort: ...

    def publish_scored(
        self,
        decision: ScoredDecision,
        initial_overlay: DecisionOverlay,
        *,
        expected_version: str | None,
    ) -> PublishResultPort: ...

    def publish_overlay(
        self,
        overlay: DecisionOverlay,
        *,
        expected_version: str | None,
    ) -> PublishResultPort: ...


__all__ = ["DecisionPublisherPort", "OverlayPublisher", "PublishResultPort"]
