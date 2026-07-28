"""Read-only tomorrow v2 decision views and matching live quote overlays."""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.ports.clock import Clock
from trader.application.ports.decision_freezes import (
    DecisionFreezeError,
    TomorrowDecisionFreezeReader,
)
from trader.domain.recommendation.tomorrow_freeze import (
    DecisionAnchor,
    TomorrowDecisionFreeze,
)
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    TomorrowDecisionEntry,
)

VIEW_SCHEMA_VERSION = "tomorrow_decision_view_v2"
STATUS_SCHEMA_VERSION = "tomorrow_status_v2"
_SHANGHAI_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class TomorrowLiveQuote:
    code: str
    price: float | None
    pct_change: float | None
    source: str
    source_time: datetime
    data_version: str

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("tomorrow live quote code must contain six digits")
        for value, label in ((self.price, "price"), (self.pct_change, "pct_change")):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"tomorrow live quote {label} must be finite")
        if self.price is not None and self.price <= 0.0:
            raise ValueError("tomorrow live quote price must be positive")
        if not self.source.strip() or not self.data_version.strip():
            raise ValueError("tomorrow live quote identity must not be empty")
        _require_shanghai(self.source_time, "tomorrow live quote source_time")


@dataclass(frozen=True)
class TomorrowQuoteOverlay:
    decision_version: str
    version: str
    observed_at: datetime
    quotes: tuple[TomorrowLiveQuote, ...]

    def __post_init__(self) -> None:
        if not self.decision_version.strip() or not self.version.strip():
            raise ValueError("tomorrow quote overlay identity must not be empty")
        _require_shanghai(self.observed_at, "tomorrow quote overlay observed_at")
        quotes = tuple(sorted(self.quotes, key=lambda item: item.code))
        if len({item.code for item in quotes}) != len(quotes):
            raise ValueError("tomorrow quote overlay codes must be unique")
        if any(item.source_time > self.observed_at for item in quotes):
            raise ValueError("tomorrow quote overlay cannot contain future quotes")
        if any(item.source_time.date() != self.observed_at.date() for item in quotes):
            raise ValueError("tomorrow quote overlay quotes must share its trade date")
        object.__setattr__(self, "quotes", quotes)


@dataclass(frozen=True)
class QuoteOverlayPublishResult:
    accepted: bool
    reason: str


class TomorrowTelemetryUnavailableError(RuntimeError):
    """Injected runtime telemetry is temporarily unavailable."""


class TomorrowQuoteOverlayIndex:
    """Single-current in-memory quote overlay with explicit CAS."""

    def __init__(self, decisions: CurrentDecisionIndex) -> None:
        self._lock = threading.RLock()
        self._decisions = decisions
        self._current: TomorrowQuoteOverlay | None = None

    def publish(
        self,
        overlay: TomorrowQuoteOverlay,
        *,
        expected_overlay_version: str | None,
    ) -> QuoteOverlayPublishResult:
        with self._lock:
            decision = self._decisions.snapshot().decision
            current = self._current
            rejection = _overlay_rejection(
                decision,
                current,
                overlay,
                expected_overlay_version,
            )
            if rejection is not None:
                return QuoteOverlayPublishResult(False, rejection)
            self._current = overlay
            return QuoteOverlayPublishResult(True, "accepted")

    def latest(self, decision_version: str) -> TomorrowQuoteOverlay | None:
        with self._lock:
            if self._current is None or self._current.decision_version != decision_version:
                return None
            return self._current


@dataclass(frozen=True)
class TomorrowDecisionItemView:
    code: str
    name: str
    industry: str
    board: str
    rank: int
    action: str
    action_reason: str
    disposition: str
    current_price: float | None
    current_pct_change: float | None
    quote_source: str
    quote_source_time: datetime
    quote_version: str
    quote_age_seconds: float
    anchor_price: float | None
    anchor_pct_change: float | None
    anchor_source: str | None
    anchor_source_time: datetime | None
    anchor_to_now_pct: float | None
    local_score: float
    deepseek_score: float | None
    deepseek_risk_penalty: float
    final_score: float
    fusion_mode: str
    review_outcome: str | None
    local_risk_codes: tuple[str, ...]
    deepseek_risk_codes: tuple[str, ...]


@dataclass(frozen=True)
class TomorrowViewIdentity:
    trade_date: str
    projection_version: str
    decision_version: str
    market_epoch_version: str
    feature_epoch_version: str | None
    research_epoch_version: str | None
    config_version: str
    strategy_version: str
    fusion_version: str
    projection_stage: str


@dataclass(frozen=True)
class TomorrowDecisionView:
    status: Literal["ready", "not_ready"]
    trade_date: str | None
    projection_version: str | None
    decision_version: str | None
    market_epoch_version: str | None
    feature_epoch_version: str | None
    research_epoch_version: str | None
    quote_version: str | None
    config_version: str | None
    strategy_version: str | None
    fusion_version: str | None
    projection_stage: str | None
    published_at: datetime | None
    frozen: bool
    frozen_at: datetime | None
    freeze_kind: str | None
    freeze_version: str | None
    data_age_seconds: float | None
    evaluated_count: int
    rejected_count: int
    unscored_count: int
    selected_count: int
    filter_reason_counts: tuple[tuple[str, int], ...]
    degraded_reasons: tuple[str, ...]
    items: tuple[TomorrowDecisionItemView, ...]
    etag: str | None
    schema_version: str = VIEW_SCHEMA_VERSION

    @classmethod
    def ready_identity(
        cls,
        identity: TomorrowViewIdentity,
        *,
        published_at: datetime,
        etag: str,
    ) -> TomorrowDecisionView:
        return cls(
            status="ready",
            trade_date=identity.trade_date,
            projection_version=identity.projection_version,
            decision_version=identity.decision_version,
            market_epoch_version=identity.market_epoch_version,
            feature_epoch_version=identity.feature_epoch_version,
            research_epoch_version=identity.research_epoch_version,
            quote_version=None,
            config_version=identity.config_version,
            strategy_version=identity.strategy_version,
            fusion_version=identity.fusion_version,
            projection_stage=identity.projection_stage,
            published_at=published_at,
            frozen=False,
            frozen_at=None,
            freeze_kind=None,
            freeze_version=None,
            data_age_seconds=None,
            evaluated_count=0,
            rejected_count=0,
            unscored_count=0,
            selected_count=0,
            filter_reason_counts=(),
            degraded_reasons=(),
            items=(),
            etag=etag,
        )


@dataclass(frozen=True)
class TomorrowSourceTelemetry:
    name: str
    status: str
    source_time: datetime | None
    received_at: datetime | None

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.status.strip():
            raise ValueError("tomorrow source telemetry identity must not be empty")
        for value in (self.source_time, self.received_at):
            if value is not None:
                _require_shanghai(value, "tomorrow source telemetry time")


@dataclass(frozen=True)
class TomorrowRuntimeTelemetry:
    sources: tuple[TomorrowSourceTelemetry, ...] = ()
    pipeline_latency_ms: float | None = None
    publish_latency_ms: float | None = None
    deepseek_limit: int = 168
    deepseek_used: int = 0
    deepseek_reserved: int = 0
    recent_failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if min(self.deepseek_limit, self.deepseek_used, self.deepseek_reserved) < 0:
            raise ValueError("tomorrow DeepSeek budget values cannot be negative")
        if self.deepseek_used + self.deepseek_reserved > self.deepseek_limit:
            raise ValueError("tomorrow DeepSeek used and reserved budget exceeds limit")
        for value in (self.pipeline_latency_ms, self.publish_latency_ms):
            if value is not None and (not math.isfinite(value) or value < 0.0):
                raise ValueError("tomorrow latency must be finite and non-negative")


@dataclass(frozen=True)
class TomorrowSourceStatusView:
    name: str
    status: str
    source_time: datetime | None
    received_at: datetime | None
    source_age_seconds: float | None
    receive_age_seconds: float | None


@dataclass(frozen=True)
class TomorrowStatusView:
    status: Literal["ready", "not_ready", "degraded"]
    observed_at: datetime
    decision_version: str | None
    decision_trade_date: str | None
    projection_stage: str | None
    quote_version: str | None
    decision_age_seconds: float | None
    sources: tuple[TomorrowSourceStatusView, ...]
    pipeline_latency_ms: float | None
    publish_latency_ms: float | None
    deepseek_limit: int
    deepseek_used: int
    deepseek_reserved: int
    deepseek_remaining: int
    recent_failures: tuple[str, ...]
    schema_version: str = STATUS_SCHEMA_VERSION


TelemetryProvider = Callable[[], TomorrowRuntimeTelemetry]


class TomorrowDecisionQueries:
    """Read-only application use cases for current, historical and status views."""

    def __init__(
        self,
        index: CurrentDecisionIndex,
        repository: TomorrowDecisionFreezeReader,
        clock: Clock,
        *,
        quotes: TomorrowQuoteOverlayIndex | None = None,
        telemetry: TelemetryProvider | None = None,
    ) -> None:
        self._index = index
        self._repository = repository
        self._clock = clock
        self._quotes = quotes
        self._telemetry = telemetry or TomorrowRuntimeTelemetry

    def current(self) -> TomorrowDecisionView:
        now = _now(self._clock)
        snapshot = self._index.snapshot()
        decision = snapshot.decision
        if decision is None or decision.trade_date != now.date() or decision.observed_at > now:
            return _not_ready(now.date(), ("current_decision_unavailable",))
        frozen = snapshot.frozen
        if frozen is not None and frozen.decision.version != decision.version:
            frozen = None
        overlay = self._quotes.latest(decision.version) if self._quotes is not None else None
        if overlay is not None and (overlay.observed_at.date() != now.date() or overlay.observed_at > now):
            overlay = None
        return _build_view(decision, now=now, frozen=frozen, overlay=overlay)

    def history(self, trade_date: date) -> TomorrowDecisionView:
        now = _now(self._clock)
        try:
            frozen = self._repository.load_frozen(trade_date)
        except (DecisionFreezeError, OSError):
            return _not_ready(trade_date, ("history_unavailable",))
        if frozen is None:
            return _not_ready(trade_date, ("formal_decision_unavailable",))
        if frozen.trade_date != trade_date:
            return _not_ready(trade_date, ("formal_decision_identity_mismatch",))
        return _build_view(frozen.decision, now=now, frozen=frozen, overlay=None)

    def status(self) -> TomorrowStatusView:
        now = _now(self._clock)
        decision = self._index.snapshot().decision
        overlay = self._quotes.latest(decision.version) if decision is not None and self._quotes is not None else None
        try:
            telemetry = self._telemetry()
        except (TomorrowTelemetryUnavailableError, OSError):
            telemetry = TomorrowRuntimeTelemetry(recent_failures=("runtime_telemetry_unavailable",))
        sources = tuple(
            TomorrowSourceStatusView(
                name=item.name,
                status=item.status,
                source_time=item.source_time,
                received_at=item.received_at,
                source_age_seconds=_age(now, item.source_time),
                receive_age_seconds=_age(now, item.received_at),
            )
            for item in telemetry.sources
        )
        recent_failures = tuple(telemetry.recent_failures[-20:])
        status: Literal["ready", "not_ready", "degraded"]
        if decision is None or decision.trade_date != now.date() or decision.observed_at > now:
            status = "not_ready"
        elif recent_failures or decision.degraded_reasons:
            status = "degraded"
        else:
            status = "ready"
        return TomorrowStatusView(
            status=status,
            observed_at=now,
            decision_version=decision.version if decision is not None else None,
            decision_trade_date=decision.trade_date.isoformat() if decision is not None else None,
            projection_stage=decision.projection_stage if decision is not None else None,
            quote_version=overlay.version if overlay is not None else None,
            decision_age_seconds=_age(now, decision.observed_at if decision is not None else None),
            sources=sources,
            pipeline_latency_ms=telemetry.pipeline_latency_ms,
            publish_latency_ms=telemetry.publish_latency_ms,
            deepseek_limit=telemetry.deepseek_limit,
            deepseek_used=telemetry.deepseek_used,
            deepseek_reserved=telemetry.deepseek_reserved,
            deepseek_remaining=telemetry.deepseek_limit - telemetry.deepseek_used - telemetry.deepseek_reserved,
            recent_failures=recent_failures,
        )


def _build_view(
    decision: DecisionEpoch,
    *,
    now: datetime,
    frozen: TomorrowDecisionFreeze | None,
    overlay: TomorrowQuoteOverlay | None,
) -> TomorrowDecisionView:
    overlay_quotes = {item.code: item for item in overlay.quotes} if overlay is not None else {}
    anchors = {item.code: item for item in frozen.anchors} if frozen is not None else {}
    selected = sorted((item for item in decision.entries if item.selected), key=lambda item: item.rank)[:10]
    items = tuple(
        _item_view(
            entry,
            now=now,
            quote=overlay_quotes.get(entry.code),
            anchor=anchors.get(entry.code),
        )
        for entry in selected
    )
    reasons = tuple(
        sorted(set(decision.degraded_reasons) | (set(frozen.degraded_reasons) if frozen is not None else set()))
    )
    freeze_version = frozen.version if frozen is not None else None
    quote_version = overlay.version if overlay is not None else decision.market_epoch_version
    etag = _etag(decision.version, freeze_version, quote_version)
    source_times = tuple(item.quote_source_time for item in items)
    return TomorrowDecisionView(
        status="ready",
        trade_date=decision.trade_date.isoformat(),
        projection_version=decision.version,
        decision_version=decision.version,
        market_epoch_version=decision.market_epoch_version,
        feature_epoch_version=decision.candidate_epoch_version,
        research_epoch_version=decision.research_epoch_version,
        quote_version=quote_version,
        config_version=decision.config_version,
        strategy_version=decision.strategy_version,
        fusion_version=decision.fusion_version,
        projection_stage=decision.projection_stage,
        published_at=decision.observed_at,
        frozen=frozen is not None,
        frozen_at=frozen.frozen_at if frozen is not None else None,
        freeze_kind=frozen.freeze_kind if frozen is not None else None,
        freeze_version=freeze_version,
        data_age_seconds=max((_age(now, value) or 0.0 for value in source_times), default=None),
        evaluated_count=decision.evaluated_count,
        rejected_count=decision.rejected_count,
        unscored_count=decision.unscored_count,
        selected_count=len(items),
        filter_reason_counts=tuple(sorted(decision.filter_reason_counts.items())),
        degraded_reasons=reasons,
        items=items,
        etag=etag,
    )


def _overlay_rejection(
    decision: DecisionEpoch | None,
    current: TomorrowQuoteOverlay | None,
    overlay: TomorrowQuoteOverlay,
    expected_overlay_version: str | None,
) -> str | None:
    if decision is None or overlay.decision_version != decision.version:
        return "decision_mismatch"
    if overlay.observed_at.date() != decision.trade_date or any(
        quote.source_time.date() != decision.trade_date for quote in overlay.quotes
    ):
        return "trade_date_mismatch"
    selected_codes = {item.code for item in decision.entries if item.selected}
    if any(quote.code not in selected_codes for quote in overlay.quotes):
        return "quote_scope_mismatch"
    same_decision = current is not None and current.decision_version == overlay.decision_version
    actual_version = current.version if same_decision and current is not None else None
    if actual_version != expected_overlay_version:
        return "cas_mismatch"
    if same_decision and current is not None:
        return _same_decision_overlay_rejection(current, overlay)
    return None


def _same_decision_overlay_rejection(
    current: TomorrowQuoteOverlay,
    overlay: TomorrowQuoteOverlay,
) -> str | None:
    if overlay.observed_at < current.observed_at:
        return "stale_overlay"
    if overlay.observed_at == current.observed_at and overlay.version != current.version:
        return "version_conflict"
    return None


def _item_view(
    entry: TomorrowDecisionEntry,
    *,
    now: datetime,
    quote: TomorrowLiveQuote | None,
    anchor: DecisionAnchor | None,
) -> TomorrowDecisionItemView:
    base = entry.features.quote
    source_time = quote.source_time if quote is not None else base.source_time
    current_price = quote.price if quote is not None else base.price
    anchor_price = anchor.price if anchor is not None else base.price
    return TomorrowDecisionItemView(
        code=entry.code,
        name=base.name,
        industry=base.industry,
        board=base.board.value,
        rank=entry.rank,
        action=entry.action.value,
        action_reason=entry.action_reason,
        disposition=entry.disposition.value,
        current_price=current_price,
        current_pct_change=quote.pct_change if quote is not None else base.pct_change,
        quote_source=quote.source if quote is not None else base.source,
        quote_source_time=source_time,
        quote_version=quote.data_version if quote is not None else base.data_version,
        quote_age_seconds=_age(now, source_time) or 0.0,
        anchor_price=anchor_price,
        anchor_pct_change=anchor.pct_change if anchor is not None else base.pct_change,
        anchor_source=anchor.source if anchor is not None else base.source,
        anchor_source_time=anchor.source_time if anchor is not None else base.source_time,
        anchor_to_now_pct=_price_change_pct(current_price, anchor_price),
        local_score=entry.score.local_score,
        deepseek_score=entry.score.deepseek_score,
        deepseek_risk_penalty=entry.score.deepseek_risk_penalty,
        final_score=entry.score.final_score,
        fusion_mode=entry.score.fusion_mode.value,
        review_outcome=entry.review_outcome.value if entry.review_outcome is not None else None,
        local_risk_codes=tuple(item.risk_code for item in entry.local_risk_facts[:5]),
        deepseek_risk_codes=tuple(item.risk_code for item in entry.deepseek_risk_facts[:5]),
    )


def _not_ready(trade_date: date, reasons: tuple[str, ...]) -> TomorrowDecisionView:
    return TomorrowDecisionView(
        status="not_ready",
        trade_date=trade_date.isoformat(),
        projection_version=None,
        decision_version=None,
        market_epoch_version=None,
        feature_epoch_version=None,
        research_epoch_version=None,
        quote_version=None,
        config_version=None,
        strategy_version=None,
        fusion_version=None,
        projection_stage=None,
        published_at=None,
        frozen=False,
        frozen_at=None,
        freeze_kind=None,
        freeze_version=None,
        data_age_seconds=None,
        evaluated_count=0,
        rejected_count=0,
        unscored_count=0,
        selected_count=0,
        filter_reason_counts=(),
        degraded_reasons=reasons,
        items=(),
        etag=None,
    )


def _etag(decision_version: str, freeze_version: str | None, quote_version: str) -> str:
    payload = "\x1f".join((decision_version, freeze_version or "", quote_version))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f'"tomorrow:{digest}"'


def _now(clock: Clock) -> datetime:
    value = clock.now()
    _require_shanghai(value, "tomorrow query clock")
    return value


def _require_shanghai(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    if getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{label} must use Asia/Shanghai")


def _age(now: datetime, value: datetime | None) -> float | None:
    if value is None:
        return None
    return round(max(0.0, (now - value).total_seconds()), 3)


def _price_change_pct(current: float | None, anchor: float | None) -> float | None:
    if current is None or anchor is None or anchor <= 0.0:
        return None
    return round((current / anchor - 1.0) * 100.0, 4)


__all__ = [
    "QuoteOverlayPublishResult",
    "TomorrowDecisionItemView",
    "TomorrowDecisionQueries",
    "TomorrowDecisionView",
    "TomorrowLiveQuote",
    "TomorrowQuoteOverlay",
    "TomorrowQuoteOverlayIndex",
    "TomorrowRuntimeTelemetry",
    "TomorrowSourceStatusView",
    "TomorrowSourceTelemetry",
    "TomorrowStatusView",
    "TomorrowTelemetryUnavailableError",
    "TomorrowViewIdentity",
]
