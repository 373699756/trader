"""Pure tomorrow decision checkpoint and formal freeze contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from trader.domain.recommendation.tomorrow_fusion import DecisionEpoch

CHECKPOINT_SCHEMA_VERSION = "tomorrow_checkpoint_v1"
FREEZE_SCHEMA_VERSION = "tomorrow_freeze_v1"
FreezeKind = Literal["scheduled", "checkpoint_recovery", "close_fallback"]
_CODE = re.compile(r"^\d{6}$")
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_SHANGHAI_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class DecisionAnchor:
    code: str
    price: float
    pct_change: float | None
    source: str
    source_time: datetime
    data_version: str

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("decision anchor code must contain six digits")
        if not math.isfinite(self.price) or self.price <= 0.0:
            raise ValueError("decision anchor price must be finite and positive")
        if self.pct_change is not None and not math.isfinite(self.pct_change):
            raise ValueError("decision anchor pct_change must be finite")
        if not self.source.strip() or not self.data_version.strip():
            raise ValueError("decision anchor source identity must not be empty")
        _require_shanghai_time(self.source_time, "decision anchor source_time")


@dataclass(frozen=True)
class TomorrowFreezeCheckpoint:
    decision: DecisionEpoch
    boundary_at: datetime
    schema_version: str = CHECKPOINT_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_boundary(self.boundary_at)
        if self.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(f"checkpoint schema_version must be {CHECKPOINT_SCHEMA_VERSION}")
        age = self.boundary_at - self.decision.observed_at
        if self.decision.trade_date != self.boundary_at.date():
            raise ValueError("checkpoint decision and boundary must share a trade date")
        if not timedelta(0) <= age <= timedelta(seconds=30):
            raise ValueError("checkpoint decision must be within 30 seconds before boundary")
        content_hash = _hash_payload(
            {
                "schema_version": self.schema_version,
                "decision_version": self.decision.version,
                "decision_hash": self.decision.content_hash,
                "boundary_at": self.boundary_at.isoformat(),
            }
        )
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self,
            "version",
            f"tomorrow-checkpoint:{self.trade_date.isoformat()}:{content_hash[:16]}",
        )

    @property
    def trade_date(self) -> date:
        return self.boundary_at.date()

    @property
    def decision_version(self) -> str:
        return self.decision.version


@dataclass(frozen=True)
class TomorrowDecisionFreeze:
    decision: DecisionEpoch
    frozen_at: datetime
    freeze_kind: FreezeKind
    anchors: tuple[DecisionAnchor, ...]
    checkpoint_version: str | None = None
    degraded_reasons: tuple[str, ...] = ()
    schema_version: str = FREEZE_SCHEMA_VERSION
    content_hash: str = field(init=False)
    version: str = field(init=False)

    def __post_init__(self) -> None:
        _require_shanghai_time(self.frozen_at, "tomorrow frozen_at")
        if self.schema_version != FREEZE_SCHEMA_VERSION:
            raise ValueError(f"freeze schema_version must be {FREEZE_SCHEMA_VERSION}")
        if self.decision.trade_date != self.frozen_at.date():
            raise ValueError("frozen decision and timestamp must share a trade date")
        anchors = tuple(self.anchors)
        reasons = tuple(sorted(set(self.degraded_reasons)))
        if any(_REASON_CODE.fullmatch(reason) is None for reason in reasons):
            raise ValueError("freeze degraded reasons must be structured codes")
        _validate_freeze_kind(self, reasons)
        _validate_anchors(self, anchors)
        content_hash = _hash_payload(
            {
                "schema_version": self.schema_version,
                "decision_version": self.decision.version,
                "decision_hash": self.decision.content_hash,
                "frozen_at": self.frozen_at.isoformat(),
                "freeze_kind": self.freeze_kind,
                "checkpoint_version": self.checkpoint_version,
                "degraded_reasons": reasons,
                "anchors": [_anchor_identity(anchor) for anchor in anchors],
            }
        )
        object.__setattr__(self, "anchors", anchors)
        object.__setattr__(self, "degraded_reasons", reasons)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self,
            "version",
            f"tomorrow-freeze:{self.trade_date.isoformat()}:{content_hash[:16]}",
        )

    @property
    def trade_date(self) -> date:
        return self.frozen_at.date()


def build_decision_anchors(decision: DecisionEpoch) -> tuple[DecisionAnchor, ...]:
    anchors: list[DecisionAnchor] = []
    for entry in sorted((item for item in decision.entries if item.selected), key=lambda item: item.rank):
        quote = entry.features.quote
        if quote.price is None:
            raise ValueError("selected decision anchor requires a positive price")
        anchors.append(
            DecisionAnchor(
                code=entry.code,
                price=quote.price,
                pct_change=quote.pct_change,
                source=quote.source,
                source_time=quote.source_time,
                data_version=quote.data_version,
            )
        )
    return tuple(anchors)


def _validate_freeze_kind(
    frozen: TomorrowDecisionFreeze,
    reasons: tuple[str, ...],
) -> None:
    boundary = frozen.frozen_at.replace(hour=14, minute=50, second=0, microsecond=0)
    close = frozen.frozen_at.replace(hour=15, minute=0, second=0, microsecond=0)
    if frozen.freeze_kind in {"scheduled", "checkpoint_recovery"}:
        _validate_scheduled_freeze(frozen, reasons, boundary)
    elif frozen.freeze_kind == "close_fallback":
        _validate_close_freeze(frozen, reasons, close)
    else:
        raise ValueError("unsupported tomorrow freeze kind")


def _validate_scheduled_freeze(
    frozen: TomorrowDecisionFreeze,
    reasons: tuple[str, ...],
    boundary: datetime,
) -> None:
    if frozen.frozen_at != boundary or frozen.decision.observed_at > boundary:
        raise ValueError("scheduled freeze must use the 14:50 decision boundary")
    if frozen.freeze_kind == "checkpoint_recovery" and not frozen.checkpoint_version:
        raise ValueError("checkpoint recovery requires checkpoint_version")
    if frozen.freeze_kind == "scheduled" and frozen.checkpoint_version is not None:
        raise ValueError("scheduled freeze cannot reference checkpoint_version")
    if {"close_fallback", "official_close"} & set(reasons):
        raise ValueError("scheduled freeze cannot contain close fallback reasons")


def _validate_close_freeze(
    frozen: TomorrowDecisionFreeze,
    reasons: tuple[str, ...],
    close: datetime,
) -> None:
    if frozen.frozen_at < close or frozen.decision.observed_at > frozen.frozen_at:
        raise ValueError("close fallback must occur at or after 15:00 without future decisions")
    if frozen.checkpoint_version is not None:
        raise ValueError("close fallback cannot reference checkpoint_version")
    if not {"close_fallback", "official_close"}.issubset(reasons):
        raise ValueError("close fallback requires official close reasons")
    if frozen.decision.projection_stage == "local" and "local_only" not in reasons:
        raise ValueError("local close fallback must declare local_only")


def _validate_anchors(
    frozen: TomorrowDecisionFreeze,
    anchors: tuple[DecisionAnchor, ...],
) -> None:
    selected_codes = tuple(
        item.code
        for item in sorted((item for item in frozen.decision.entries if item.selected), key=lambda item: item.rank)
    )
    anchor_codes = tuple(anchor.code for anchor in anchors)
    if anchor_codes != selected_codes:
        raise ValueError("anchors must exactly match selected decision codes")
    if len(set(anchor_codes)) != len(anchor_codes):
        raise ValueError("decision anchors must contain unique codes")
    if any(anchor.source_time > frozen.frozen_at for anchor in anchors):
        raise ValueError("freeze cannot contain a future anchor")
    if frozen.freeze_kind == "close_fallback" and any(anchor.source != "official_close" for anchor in anchors):
        raise ValueError("close fallback anchors must use official_close source")


def _validate_boundary(value: datetime) -> None:
    _require_shanghai_time(value, "tomorrow freeze boundary")
    if (value.hour, value.minute, value.second, value.microsecond) != (14, 50, 0, 0):
        raise ValueError("tomorrow freeze boundary must be exactly 14:50")


def _require_shanghai_time(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    if getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{label} must use Asia/Shanghai")


def _anchor_identity(anchor: DecisionAnchor) -> dict[str, object]:
    return {
        "code": anchor.code,
        "price": anchor.price,
        "pct_change": anchor.pct_change,
        "source": anchor.source,
        "source_time": anchor.source_time.isoformat(),
        "data_version": anchor.data_version,
    }


def _hash_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "FREEZE_SCHEMA_VERSION",
    "DecisionAnchor",
    "FreezeKind",
    "TomorrowDecisionFreeze",
    "TomorrowFreezeCheckpoint",
    "build_decision_anchors",
]
