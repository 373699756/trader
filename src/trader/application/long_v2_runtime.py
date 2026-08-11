"""Current-only Long V2 quote lane and unified projection publication."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Literal

from trader.application.cache import request_fingerprint
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.long_groups import LongGroupDefinition, LongWatchItemDefinition
from trader.application.ports.long import LongRefreshRequest
from trader.application.ports.market import MarketDataUnavailableError, QuoteReaderPort
from trader.application.schedule import SHANGHAI
from trader.application.shutdown import ShutdownDeadline, ShutdownStep
from trader.application.v2_lifecycle import LatestWinsStatus, LatestWinsWorker
from trader.domain.market.models import FeatureSnapshot, MarketQuote
from trader.domain.recommendation.decision_identity import LongProjection, LongProjectionItem
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class LongV2RuntimeDependencies:
    quotes: QuoteReaderPort
    index: UnifiedDecisionIndex
    now: Callable[[], datetime]
    publish_projection: Callable[[LongProjection], object] = lambda _projection: None


@dataclass(frozen=True)
class LongV2RuntimeStatus:
    worker: LatestWinsStatus
    score_status: Literal["not_applicable"]
    published_count: int
    publish_rejection_count: int
    input_rejection_count: int
    fetch_failure_count: int
    live_count: int
    retained_count: int
    missing_count: int
    degraded_reasons: tuple[str, ...]
    last_error_code: str


class LongV2Runtime:
    """Own one latest-wins targeted quote task and publish no-score current projections."""

    def __init__(
        self,
        dependencies: LongV2RuntimeDependencies,
        *,
        config_version: str,
        watchlist_version: str,
        items: tuple[LongWatchItemDefinition, ...],
        groups: tuple[LongGroupDefinition, ...],
    ) -> None:
        if not config_version or not watchlist_version:
            raise ValueError("long runtime versions must not be empty")
        self._items = _validate_items(items)
        self._groups = _group_ownership(self._items, groups)
        self._codes = tuple(item.code for item in self._items)
        self._quotes = dependencies.quotes
        self._index = dependencies.index
        self._now = dependencies.now
        self._publish_projection_event = dependencies.publish_projection
        self._config_version = config_version
        self._watchlist_version = watchlist_version
        self._lock = threading.RLock()
        self._sequence = 1
        self._retained_trade_date: date | None = None
        self._retained_quotes: dict[str, MarketQuote] = {}
        self._last_observed_at: datetime | None = None
        self._published_count = 0
        self._publish_rejection_count = 0
        self._input_rejection_count = 0
        self._fetch_failure_count = 0
        self._live_count = 0
        self._retained_count = 0
        self._missing_count = len(self._items)
        self._degraded_reasons: tuple[str, ...] = ()
        self._last_error_code = ""
        self._worker = LatestWinsWorker(
            "trader-v2-long",
            self._process,
            order_key=lambda request: int(request.observed_at.timestamp() * 1_000_000),
        )

    @property
    def codes(self) -> tuple[str, ...]:
        return self._codes

    def start(self) -> bool:
        return self._worker.start()

    def offer_refresh(self, request: LongRefreshRequest) -> bool:
        return self._worker.offer(request).value in {"accepted", "replaced", "coalesced"}

    def wait_idle(self, timeout_seconds: float) -> bool:
        return self._worker.wait_idle(timeout_seconds)

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        if not wait:
            self._worker.close()
            return ShutdownStep("trader-v2-long-close", True, False)
        return self._worker.stop(deadline=deadline or ShutdownDeadline.start(30.0))

    def status(self) -> LongV2RuntimeStatus:
        with self._lock:
            return LongV2RuntimeStatus(
                self._worker.status(),
                "not_applicable",
                self._published_count,
                self._publish_rejection_count,
                self._input_rejection_count,
                self._fetch_failure_count,
                self._live_count,
                self._retained_count,
                self._missing_count,
                self._degraded_reasons,
                self._last_error_code,
            )

    def _process(self, request: LongRefreshRequest) -> None:
        with self._lock:
            if self._last_observed_at is not None and request.observed_at < self._last_observed_at:
                self._input_rejection_count += 1
                self._last_error_code = "input:stale_observation"
                return
        degraded_reasons: list[str] = []
        try:
            features = tuple(
                self._quotes.refresh_long_quotes(
                    self._codes,
                    request.observed_at,
                    force=request.force,
                    deadline=request.deadline,
                )
            )
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError):
            features = ()
            degraded_reasons.append("long_quote_unavailable")
            with self._lock:
                self._fetch_failure_count += 1
                self._last_error_code = "long_quote_unavailable"
        completed_at = max(request.observed_at, _shanghai(self._now()))
        fresh = _fresh_quotes(features, frozenset(self._codes), completed_at)
        items, next_retained, live_count, retained_count, missing_count = self._projection_items(
            fresh,
            completed_at,
        )
        if live_count != len(self._codes):
            degraded_reasons.append("long_quotes_partial")
        projection = LongProjection(
            trade_date=completed_at.date(),
            sequence=self._next_sequence(),
            observed_at=completed_at,
            input_versions=(
                ("config", self._config_version),
                ("long_quotes", _quote_input_version(fresh, request)),
                ("watchlist", self._watchlist_version),
            ),
            items=items,
        )
        expected = self._index.snapshot(Strategy.LONG).current
        published = self._index.publish(
            projection,
            expected_version=expected.version if expected is not None else None,
        )
        with self._lock:
            self._live_count = live_count
            self._retained_count = retained_count
            self._missing_count = missing_count
            self._degraded_reasons = tuple(dict.fromkeys(degraded_reasons))
            if published.accepted:
                self._retained_trade_date = completed_at.date()
                self._retained_quotes = next_retained
                self._last_observed_at = request.observed_at
                self._published_count += 1
            else:
                self._publish_rejection_count += 1
                self._last_error_code = f"publish:{published.reason}"
        if published.accepted:
            self._publish_projection_event(projection)

    def _projection_items(
        self,
        fresh: dict[str, MarketQuote],
        observed_at: datetime,
    ) -> tuple[tuple[LongProjectionItem, ...], dict[str, MarketQuote], int, int, int]:
        trade_date = observed_at.date()
        with self._lock:
            retained = dict(self._retained_quotes) if self._retained_trade_date == trade_date else {}
        accepted_fresh: dict[str, MarketQuote] = {}
        for code, candidate_quote in fresh.items():
            previous = retained.get(code)
            if previous is None:
                retained[code] = candidate_quote
                accepted_fresh[code] = candidate_quote
                continue
            candidate_order = _quote_order(candidate_quote)
            previous_order = _quote_order(previous)
            if candidate_order > previous_order or (candidate_order == previous_order and candidate_quote == previous):
                retained[code] = candidate_quote
                accepted_fresh[code] = candidate_quote
        projection_items: list[LongProjectionItem] = []
        live_count = 0
        retained_count = 0
        missing_count = 0
        for item in self._items:
            quote = accepted_fresh.get(item.code)
            quote_status: Literal["live", "retained", "missing"] = "live"
            if quote is None:
                quote = retained.get(item.code)
                quote_status = "retained" if quote is not None else "missing"
            if quote is None:
                missing_count += 1
                projection_items.append(
                    LongProjectionItem(
                        item.code,
                        self._groups[item.code],
                        f"missing:{self._watchlist_version}",
                        name=item.name,
                        industry=item.industry,
                    )
                )
                continue
            if quote_status == "live":
                live_count += 1
            else:
                retained_count += 1
            available_status: Literal["live", "retained"] = "live" if quote_status == "live" else "retained"
            projection_items.append(_projection_item(item, self._groups[item.code], quote, available_status))
        return tuple(projection_items), retained, live_count, retained_count, missing_count

    def _next_sequence(self) -> int:
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
            return sequence


def _validate_items(items: tuple[LongWatchItemDefinition, ...]) -> tuple[LongWatchItemDefinition, ...]:
    normalized = tuple(items)
    codes = tuple(item.code for item in normalized)
    if not normalized or len(codes) != len(set(codes)):
        raise ValueError("long runtime requires a non-empty unique fixed watchlist")
    return normalized


def _group_ownership(
    items: tuple[LongWatchItemDefinition, ...],
    groups: tuple[LongGroupDefinition, ...],
) -> dict[str, str]:
    allowed = {item.code for item in items}
    ownership: dict[str, str] = {}
    for index, group in enumerate(groups, start=1):
        group_id = f"long-group:{index:03d}"
        for code in group.codes:
            if code not in allowed or code in ownership:
                raise ValueError("each watchlist code must belong to exactly one long group")
            ownership[code] = group_id
    if set(ownership) != allowed:
        raise ValueError("each watchlist code must belong to exactly one long group")
    return ownership


def _fresh_quotes(
    features: tuple[FeatureSnapshot, ...],
    allowed_codes: frozenset[str],
    observed_at: datetime,
) -> dict[str, MarketQuote]:
    candidates: dict[str, list[MarketQuote]] = {}
    for feature in features:
        quote = getattr(feature, "quote", None)
        if not isinstance(quote, MarketQuote):
            continue
        try:
            source_time = _shanghai(quote.source_time)
            received_time = _shanghai(quote.received_time)
        except ValueError:
            continue
        if (
            quote.code not in allowed_codes
            or source_time > observed_at
            or received_time > observed_at
            or quote.price is None
            or not math.isfinite(quote.price)
            or quote.price <= 0.0
            or not quote.source
            or not quote.data_version
        ):
            continue
        normalized = replace(
            quote,
            source_time=source_time,
            received_time=received_time,
            pct_change=_finite_or_none(quote.pct_change),
            amount=_non_negative_or_none(quote.amount),
            turnover_rate=_non_negative_or_none(quote.turnover_rate),
            market_cap=_non_negative_or_none(quote.market_cap),
        )
        candidates.setdefault(quote.code, []).append(normalized)
    selected: dict[str, MarketQuote] = {}
    for code, values in candidates.items():
        newest_order = max(_quote_order(value) for value in values)
        newest = tuple(value for value in values if _quote_order(value) == newest_order)
        if any(value != newest[0] for value in newest[1:]):
            continue
        selected[code] = newest[0]
    return selected


def _quote_order(quote: MarketQuote) -> tuple[datetime, datetime, str]:
    return quote.source_time, quote.received_time, quote.data_version


def _projection_item(
    item: LongWatchItemDefinition,
    group: str,
    quote: MarketQuote,
    quote_status: Literal["live", "retained"],
) -> LongProjectionItem:
    return LongProjectionItem(
        item.code,
        group,
        quote.data_version,
        name=item.name,
        industry=item.industry,
        price=quote.price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        market_cap=quote.market_cap,
        source=quote.source,
        source_time=quote.source_time,
        quote_status=quote_status,
    )


def _quote_input_version(features: dict[str, MarketQuote], request: LongRefreshRequest) -> str:
    quotes = tuple(
        (
            quote.code,
            quote.data_version,
            quote.source_time,
            quote.received_time,
            quote.price,
            quote.pct_change,
            quote.amount,
            quote.turnover_rate,
            quote.market_cap,
        )
        for quote in sorted(features.values(), key=lambda value: value.code)
    )
    material = {
        "observed_at": request.observed_at,
        "phase": request.phase,
        "quotes": quotes,
    }
    return f"long-quotes:{request_fingerprint(material)[:24]}"


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _non_negative_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value >= 0.0 else None


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("long quote time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = ["LongV2Runtime", "LongV2RuntimeDependencies", "LongV2RuntimeStatus"]
