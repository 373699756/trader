"""Thread-safe current, formal, and quote-overlay snapshot publisher."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    BuildFreezeCommitted,
    FreezeCommitted,
)
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    DisplayQuoteCodes,
    FormalScoredFreeze,
    FreezeIdentity,
    FreezeOverlay,
    IdentityCodes,
    IsPreviewFreeze,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class UnifiedFreezePublishResult:
    accepted: bool
    reason: str
    event: FreezeCommitted | None = None


@dataclass(frozen=True)
class UnifiedPairedPublishResult:
    accepted: bool
    reason: str
    events: tuple[FreezeCommitted, ...] = ()


@dataclass(frozen=True)
class UnifiedOverlayPublishResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class UnifiedFreezeSnapshot:
    current: FreezeIdentity | None
    overlay: FreezeOverlay | None
    formal: CommittedFreezeRecord | None


@dataclass(frozen=True)
class UnifiedFreezeSealResult:
    accepted: bool
    reason: str
    freeze: ScoredFreeze | None = None
    source: Literal["current", "explicit"] | None = None


@dataclass(frozen=True)
class _UnifiedFreezeSeal:
    boundary_at: datetime
    source_version: str
    freeze: ScoredFreeze
    source: Literal["current", "explicit"]


@dataclass(frozen=True)
class _ScoredPublishCheck:
    current: FreezeIdentity | None
    seal: _UnifiedFreezeSeal | None
    closed: tuple[date, datetime] | None
    freeze: ScoredFreeze
    overlay: FreezeOverlay
    expected_version: str | None


class UnifiedFreezeIndex:
    """Expected-version CAS for scored currents, overlays, and formal records."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: dict[Strategy, FreezeIdentity] = {}
        self._overlays: dict[Strategy, FreezeOverlay] = {}
        self._seals: dict[Strategy, _UnifiedFreezeSeal] = {}
        self._formal: dict[Strategy, CommittedFreezeRecord] = {}
        self._closed: dict[Strategy, tuple[date, datetime]] = {}

    def publish(
        self,
        identity: FreezeIdentity,
        *,
        expected_version: str | None,
    ) -> UnifiedFreezePublishResult:
        with self._lock:
            current = self._current.get(identity.strategy)
            seal = self._seals.get(identity.strategy)
            if seal is not None and seal.freeze.trade_date == identity.trade_date:
                return UnifiedFreezePublishResult(False, "freeze_sealed")
            closed = self._closed.get(identity.strategy)
            if closed is not None and closed[0] == identity.trade_date:
                return UnifiedFreezePublishResult(False, "freeze_closed")
            rejection = _identity_rejection(current, identity, expected_version)
            if rejection is not None:
                return UnifiedFreezePublishResult(False, rejection)
            self._current[identity.strategy] = identity
            if current is not None and identity.trade_date > current.trade_date:
                self._seals.pop(identity.strategy, None)
                self._formal.pop(identity.strategy, None)
                self._closed.pop(identity.strategy, None)
            overlay = self._overlays.get(identity.strategy)
            if overlay is not None and overlay.parent_version != identity.version:
                self._overlays.pop(identity.strategy, None)
            event = BuildFreezeCommitted(identity) if isinstance(identity, ScoredFreeze) else None
            return UnifiedFreezePublishResult(True, "accepted", event)

    def publish_scored(
        self,
        freeze: ScoredFreeze,
        initial_overlay: FreezeOverlay,
        *,
        expected_version: str | None,
    ) -> UnifiedFreezePublishResult:
        """Publish a scored identity and its complete same-batch quote view atomically."""

        with self._lock:
            current = self._current.get(freeze.strategy)
            rejection = _scored_rejection(
                _ScoredPublishCheck(
                    current,
                    self._seals.get(freeze.strategy),
                    self._closed.get(freeze.strategy),
                    freeze,
                    initial_overlay,
                    expected_version,
                )
            )
            if rejection is not None:
                return UnifiedFreezePublishResult(False, rejection)
            self._current[freeze.strategy] = freeze
            self._overlays[freeze.strategy] = initial_overlay
            if current is not None and freeze.trade_date > current.trade_date:
                self._seals.pop(freeze.strategy, None)
                self._formal.pop(freeze.strategy, None)
                self._closed.pop(freeze.strategy, None)
            projection_version = f"{freeze.version}+{initial_overlay.version}"
            return UnifiedFreezePublishResult(
                True,
                "accepted",
                BuildFreezeCommitted(freeze, projection_version=projection_version),
            )

    def publish_scored_pair(
        self,
        freezes: tuple[ScoredFreeze, ScoredFreeze],
        overlays: tuple[FreezeOverlay, FreezeOverlay],
        expected_versions: tuple[str | None, str | None],
    ) -> UnifiedPairedPublishResult:
        """Validate both scored currents before changing either strategy."""

        if tuple(freeze.strategy for freeze in freezes) != (Strategy.TOMORROW, Strategy.D25):
            raise ValueError("paired publication requires tomorrow and d25 in order")
        if freezes[0].trade_date != freezes[1].trade_date:
            raise ValueError("paired publication requires one trade date")
        with self._lock:
            for freeze, overlay, expected_version in zip(freezes, overlays, expected_versions, strict=True):
                rejection = _scored_rejection(
                    _ScoredPublishCheck(
                        self._current.get(freeze.strategy),
                        self._seals.get(freeze.strategy),
                        self._closed.get(freeze.strategy),
                        freeze,
                        overlay,
                        expected_version,
                    )
                )
                if rejection is not None:
                    return UnifiedPairedPublishResult(False, rejection)
            events = tuple(
                BuildFreezeCommitted(freeze, projection_version=f"{freeze.version}+{overlay.version}")
                for freeze, overlay in zip(freezes, overlays, strict=True)
            )
            for freeze, overlay in zip(freezes, overlays, strict=True):
                current = self._current.get(freeze.strategy)
                self._current[freeze.strategy] = freeze
                self._overlays[freeze.strategy] = overlay
                if current is not None and freeze.trade_date > current.trade_date:
                    self._seals.pop(freeze.strategy, None)
                    self._formal.pop(freeze.strategy, None)
                    self._closed.pop(freeze.strategy, None)
            return UnifiedPairedPublishResult(True, "accepted", (events[0], events[1]))

    def publish_overlay(
        self,
        overlay: FreezeOverlay,
        *,
        expected_version: str | None,
    ) -> UnifiedOverlayPublishResult:
        with self._lock:
            current = self._current.get(overlay.strategy)
            existing = self._overlays.get(overlay.strategy)
            rejection = _overlay_rejection(current, existing, overlay, expected_version)
            if rejection is not None:
                return UnifiedOverlayPublishResult(False, rejection)
            self._overlays[overlay.strategy] = overlay
            return UnifiedOverlayPublishResult(True, "accepted")

    @property
    def next_scored_sequence(self) -> int:
        with self._lock:
            return (
                max(
                    (
                        freeze.sequence
                        for strategy in (Strategy.TOMORROW, Strategy.D25)
                        if isinstance((freeze := self._current.get(strategy)), ScoredFreeze)
                    ),
                    default=0,
                )
                + 1
            )

    def snapshot(self, strategy: Strategy) -> UnifiedFreezeSnapshot:
        with self._lock:
            return UnifiedFreezeSnapshot(
                self._current.get(strategy),
                self._overlays.get(strategy),
                self._formal.get(strategy),
            )

    def is_sealed(self, strategy: Strategy, trade_date: date) -> bool:
        with self._lock:
            seal = self._seals.get(strategy)
            return seal is not None and seal.freeze.trade_date == trade_date

    def is_closed(self, strategy: Strategy, trade_date: date) -> bool:
        with self._lock:
            closed = self._closed.get(strategy)
            return closed is not None and closed[0] == trade_date

    def close_for_date(self, strategy: Strategy, trade_date: date, *, boundary_at: datetime) -> bool:
        with self._lock:
            if (
                boundary_at.tzinfo is None
                or boundary_at.utcoffset() is None
                or not (isinstance(boundary_at.tzinfo, ZoneInfo) and boundary_at.tzinfo.key == "Asia/Shanghai")
            ):
                raise ValueError("freeze close boundary must use Asia/Shanghai")
            if boundary_at.date() != trade_date:
                raise ValueError("freeze close boundary must match trade date")
            existing = self._closed.get(strategy)
            if existing is not None and existing[0] == trade_date:
                return existing[1] == boundary_at
            if existing is not None and existing[0] > trade_date:
                return False
            self._closed[strategy] = (trade_date, boundary_at)
            return True

    def discard_closed_current(self, strategy: Strategy, trade_date: date) -> bool:
        with self._lock:
            if not self.is_closed(strategy, trade_date) or self.is_sealed(strategy, trade_date):
                return False
            formal = self._formal.get(strategy)
            if formal is not None and formal.trade_date == trade_date:
                return False
            current = self._current.get(strategy)
            if current is not None and current.trade_date == trade_date:
                self._current.pop(strategy, None)
                self._overlays.pop(strategy, None)
            return True

    def seal_for_freeze(
        self,
        strategy: Strategy,
        *,
        boundary_at: datetime,
    ) -> UnifiedFreezeSealResult:
        with self._lock:
            current = self._current.get(strategy)
            if isinstance(current, ScoredFreeze) and IsPreviewFreeze(current):
                return UnifiedFreezeSealResult(False, "preview_only")
            existing = self._seals.get(strategy)
            if existing is not None:
                if existing.boundary_at == boundary_at:
                    return UnifiedFreezeSealResult(
                        True,
                        "already_sealed",
                        existing.freeze,
                        existing.source,
                    )
                return UnifiedFreezeSealResult(False, "different_boundary")
            current = self._current.get(strategy)
            if (
                isinstance(current, ScoredFreeze)
                and current.trade_date == boundary_at.date()
                and current.observed_at <= boundary_at
            ):
                return self._seal(current, boundary_at, "current")
            return UnifiedFreezeSealResult(False, "no_eligible_freeze")

    def seal_close_fallback(
        self,
        freeze: ScoredFreeze,
        *,
        boundary_at: datetime,
        official_close_version: str,
    ) -> UnifiedFreezeSealResult:
        if IsPreviewFreeze(freeze):
            return UnifiedFreezeSealResult(False, "preview_only")
        with self._lock:
            existing = self._seals.get(freeze.strategy)
            if existing is not None:
                if (
                    existing.source == "explicit"
                    and existing.source_version == freeze.version
                    and existing.boundary_at == boundary_at
                    and ("official_close", official_close_version) in existing.freeze.input_versions
                ):
                    return UnifiedFreezeSealResult(
                        True,
                        "already_sealed",
                        existing.freeze,
                        existing.source,
                    )
                return UnifiedFreezeSealResult(False, "scheduled_freeze_pending")
            reasons = ["close_fallback", "official_close"]
            if freeze.stage == "local":
                reasons.append("local_only")
            return self._seal(
                freeze,
                boundary_at,
                "explicit",
                degraded_reasons=tuple(reasons),
                input_versions=(("official_close", official_close_version),),
            )

    def commit_formal(self, record: CommittedFreezeRecord) -> bool:
        if IsPreviewFreeze(record.freeze):
            return False
        with self._lock:
            existing = self._formal.get(record.strategy)
            if existing is not None:
                return existing.version == record.version and existing.payload_hash == record.payload_hash
            seal = self._seals.get(record.strategy)
            if seal is None or seal.freeze.version != record.freeze.version:
                return False
            self._current[record.strategy] = record.freeze
            self._overlays.pop(record.strategy, None)
            self._formal[record.strategy] = record
            self._closed[record.strategy] = (record.trade_date, record.committed_at)
            return True

    def restore_formal(self, record: CommittedFreezeRecord) -> bool:
        if IsPreviewFreeze(record.freeze):
            return False
        with self._lock:
            current = self._current.get(record.strategy)
            if current is not None and current.trade_date > record.trade_date:
                return False
            existing = self._formal.get(record.strategy)
            if (
                existing is not None
                and existing.trade_date == record.trade_date
                and (existing.version != record.version or existing.payload_hash != record.payload_hash)
            ):
                return False
            self._current[record.strategy] = record.freeze
            self._overlays.pop(record.strategy, None)
            self._formal[record.strategy] = record
            self._seals[record.strategy] = _UnifiedFreezeSeal(
                record.committed_at,
                record.freeze.version,
                record.freeze,
                "explicit" if record.commit_kind == "close_fallback" else "current",
            )
            self._closed[record.strategy] = (record.trade_date, record.committed_at)
            return True

    def _seal(
        self,
        freeze: ScoredFreeze,
        boundary_at: datetime,
        source: Literal["current", "explicit"],
        *,
        degraded_reasons: tuple[str, ...] = (),
        input_versions: tuple[tuple[str, str], ...] = (),
    ) -> UnifiedFreezeSealResult:
        formal = FormalScoredFreeze(
            freeze,
            degraded_reasons=degraded_reasons,
            input_versions=input_versions,
        )
        self._seals[freeze.strategy] = _UnifiedFreezeSeal(
            boundary_at,
            freeze.version,
            formal,
            source,
        )
        return UnifiedFreezeSealResult(True, "sealed", formal, source)


def _identity_rejection(
    current: FreezeIdentity | None,
    candidate: FreezeIdentity,
    expected_version: str | None,
) -> str | None:
    actual_version = current.version if current is not None else None
    if actual_version != expected_version:
        return "cas_mismatch"
    if current is None:
        return _hybrid_parent_rejection(None, candidate)
    if candidate.trade_date < current.trade_date:
        return "stale_trade_date"
    if candidate.trade_date == current.trade_date:
        if candidate.sequence < current.sequence:
            return "stale_sequence"
        if candidate.sequence == current.sequence and candidate != current:
            return "sequence_conflict"
    return _hybrid_parent_rejection(current, candidate)


def _hybrid_parent_rejection(
    current: FreezeIdentity | None,
    candidate: FreezeIdentity,
) -> str | None:
    if not isinstance(candidate, ScoredFreeze) or candidate.stage != "hybrid":
        return None
    if (
        not isinstance(current, ScoredFreeze)
        or current.stage != "local"
        or current.strategy is not candidate.strategy
        or current.trade_date != candidate.trade_date
        or candidate.parent_version != current.version
    ):
        return "parent_mismatch"
    return None


def _scored_rejection(check: _ScoredPublishCheck) -> str | None:
    if check.seal is not None and check.seal.freeze.trade_date == check.freeze.trade_date:
        return "freeze_sealed"
    if check.closed is not None and check.closed[0] == check.freeze.trade_date:
        return "freeze_closed"
    return _identity_rejection(check.current, check.freeze, check.expected_version) or _initial_overlay_rejection(
        check.freeze, check.overlay
    )


def _overlay_rejection(
    current: FreezeIdentity | None,
    existing: FreezeOverlay | None,
    candidate: FreezeOverlay,
    expected_version: str | None,
) -> str | None:
    rejection: str | None = None
    if current is None or current.version != candidate.parent_version:
        rejection = "parent_mismatch"
    elif current.trade_date != candidate.trade_date:
        rejection = "trade_date_mismatch"
    elif candidate.observed_at < current.observed_at:
        rejection = "stale_overlay"
    elif any(
        quote.code
        not in IdentityCodes(current)
        | (DisplayQuoteCodes(current) if isinstance(current, ScoredFreeze) else frozenset())
        for quote in candidate.quotes
    ):
        rejection = "quote_scope_mismatch"
    elif (existing.version if existing is not None else None) != expected_version:
        rejection = "overlay_cas_mismatch"
    elif existing is not None and (
        candidate.observed_at < existing.observed_at or candidate.sequence < existing.sequence
    ):
        rejection = "stale_overlay"
    elif existing is not None and candidate.sequence == existing.sequence and candidate != existing:
        rejection = "overlay_conflict"
    return rejection


def _initial_overlay_rejection(freeze: ScoredFreeze, candidate: FreezeOverlay) -> str | None:
    rejection: str | None = None
    if candidate.strategy is not freeze.strategy or candidate.parent_version != freeze.version:
        rejection = "parent_mismatch"
    elif candidate.trade_date != freeze.trade_date:
        rejection = "trade_date_mismatch"
    elif candidate.observed_at < freeze.observed_at:
        rejection = "stale_overlay"
    else:
        anchors = tuple(item.quote for item in freeze.items if item.selected)
        complete_anchors = tuple(anchor for anchor in anchors if anchor is not None)
        if len(complete_anchors) != len(anchors):
            rejection = "quote_anchor_missing"
        elif frozenset(quote.code for quote in candidate.quotes) != IdentityCodes(freeze):
            rejection = "quote_scope_mismatch"
        elif candidate.quotes != tuple(sorted(complete_anchors, key=lambda quote: quote.code)):
            rejection = "quote_identity_mismatch"
    return rejection


__all__ = [
    "UnifiedFreezeIndex",
    "UnifiedFreezePublishResult",
    "UnifiedPairedPublishResult",
    "UnifiedFreezeSealResult",
    "UnifiedFreezeSnapshot",
    "UnifiedOverlayPublishResult",
]
