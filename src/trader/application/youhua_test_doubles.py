"""Isolation-only test doubles for youhua phase-2 handoffs."""

from __future__ import annotations

from dataclasses import dataclass

from trader.application.ports.youhua import (
    FeatureSnapshotEnvelope,
    HighValueReviewManifest,
    MarketChangeSet,
    OverlayEvent,
    ProjectionEvent,
)


@dataclass(frozen=True)
class P4ConsumerReceipt:
    merge_epoch: str
    content_hash: str
    feature_count: int
    changed_codes: tuple[str, ...]
    dirty_field_families: tuple[str, ...]


class P4ConsumerStub:
    """Records public P3 -> P4 identity only; it does not score or publish."""

    def __init__(self) -> None:
        self._receipts: list[P4ConsumerReceipt] = []

    def consume(self, envelope: FeatureSnapshotEnvelope) -> P4ConsumerReceipt:
        receipt = P4ConsumerReceipt(
            merge_epoch=envelope.merge_epoch,
            content_hash=envelope.content_hash,
            feature_count=len(envelope.feature_snapshots),
            changed_codes=envelope.market_change_set.dirty_codes,
            dirty_field_families=envelope.market_change_set.dirty_field_families,
        )
        self._receipts.append(receipt)
        return receipt

    @property
    def receipts(self) -> tuple[P4ConsumerReceipt, ...]:
        return tuple(self._receipts)


class ReviewInputStub:
    """Records C-facing review manifests without invoking DeepSeek."""

    def __init__(self) -> None:
        self._manifests: list[HighValueReviewManifest] = []
        self._change_sets: list[MarketChangeSet] = []

    def record_market_change_set(self, change_set: MarketChangeSet) -> None:
        self._change_sets.append(change_set)

    def collect(self, manifest: HighValueReviewManifest) -> tuple[str, ...]:
        self._manifests.append(manifest)
        return tuple(item.candidate_code for item in manifest.inputs)

    @property
    def manifests(self) -> tuple[HighValueReviewManifest, ...]:
        return tuple(self._manifests)

    @property
    def change_sets(self) -> tuple[MarketChangeSet, ...]:
        return tuple(self._change_sets)


class ProjectionOverlayProducerStub:
    """Records D-facing projection and overlay events without touching Web or DOM."""

    def __init__(self) -> None:
        self._projections: list[ProjectionEvent] = []
        self._overlays: list[OverlayEvent] = []

    def publish_projection(self, event: ProjectionEvent) -> str:
        self._projections.append(event)
        return event.projection_version

    def publish_overlay(self, event: OverlayEvent) -> str:
        self._overlays.append(event)
        return event.overlay_version

    @property
    def projections(self) -> tuple[ProjectionEvent, ...]:
        return tuple(self._projections)

    @property
    def overlays(self) -> tuple[OverlayEvent, ...]:
        return tuple(self._overlays)


__all__ = [
    "P4ConsumerReceipt",
    "P4ConsumerStub",
    "ProjectionOverlayProducerStub",
    "ReviewInputStub",
]
