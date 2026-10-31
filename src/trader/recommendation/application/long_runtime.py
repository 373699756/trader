"""Current-only Long quote lane and unified projection publication."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.long import LongRefreshRequest
from trader.recommendation.application.ports.market_data import MarketDataUnavailableError, QuoteReaderPort
from trader.recommendation.application.runtime.latest_wins import LatestWinsStatus, LatestWinsWorker
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.application.runtime.trading_session import SessionGuard, StaleSessionError
from trader.recommendation.domain.market.models import FeatureSnapshot, MarketQuote
from trader.recommendation.domain.publication.freeze_identity import LongProjection, LongProjectionItem
from trader.recommendation.domain.publication.long_groups import LongGroupDefinition, LongWatchItemDefinition
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext


@dataclass(frozen=True)
class LongRuntimeDependencies:
    next_sequence: Callable[[], int] = field(kw_only=True)
    quotes: QuoteReaderPort
    index: UnifiedFreezeIndex
    now: Callable[[], datetime]
    publish_projection: Callable[[LongProjection], object] = lambda _projection: None
    enabled: bool = True


@dataclass(frozen=True)
class LongRuntimeStatus:
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


class LongRuntime:
    """Own one latest-wins targeted quote task and publish no-score current projections."""

    def __init__(
        self,
        dependencies: LongRuntimeDependencies,
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
        self._enabled = dependencies.enabled
        self._lock = threading.RLock()
        self._next_sequence = dependencies.next_sequence
        self._retained_trade_date: date | None = None
        self._retained_quotes: dict[str, MarketQuote] = {}
        self._last_observed_at: datetime | None = None
        self._last_generation = 0
        self._published_count = 0
        self._publish_rejection_count = 0
        self._input_rejection_count = 0
        self._fetch_failure_count = 0
        self._live_count = 0
        self._retained_count = 0
        self._missing_count = len(self._items)
        self._degraded_reasons: tuple[str, ...] = () if self._codes else ("long_watchlist_empty",)
        self._last_error_code = ""
        self._worker = LatestWinsWorker(
            "trader-long",
            self._process,
            order_key=lambda request: (
                (request.session.generation if request.session is not None else 0) * 10**24
                + int(request.observed_at.timestamp() * 1_000_000)
            ),
        )

    @property
    def codes(self) -> tuple[str, ...]:
        return self._codes

    def start(self) -> bool:
        if not self._enabled:
            return False
        return self._worker.start()

    def offer_refresh(self, request: LongRefreshRequest) -> bool:
        if not self._enabled:
            return True
        if not self._codes:
            return False
        return self._worker.offer(request).value in {"accepted", "replaced", "coalesced"}

    def wait_idle(self, timeout_seconds: float) -> bool:
        if not self._enabled:
            return True
        return self._worker.wait_idle(timeout_seconds)

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        if not self._enabled:
            return ShutdownStep("trader-long-disabled", True, False)
        if not wait:
            self._worker.close()
            return ShutdownStep("trader-long-close", True, False)
        if deadline is None:
            raise ValueError("long runtime wait requires a shared shutdown deadline")
        return self._worker.stop(deadline=deadline)

    def status(self) -> LongRuntimeStatus:
        with self._lock:
            return LongRuntimeStatus(
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
        try:
            self._execute(request)
        except StaleSessionError:
            return

    def _execute(self, request: LongRefreshRequest) -> None:
        with SessionGuard(request.session), self._lock:
            generation = request.session.generation if request.session is not None else 0
            if (
                generation == self._last_generation
                and self._last_observed_at is not None
                and request.observed_at < self._last_observed_at
            ):
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
                    preview_context=request.preview_context,
                )
            )
        except (MarketDataUnavailableError, OSError, RuntimeError, TypeError, ValueError):
            features = ()
            degraded_reasons.append("long_quote_unavailable")
            with SessionGuard(request.session), self._lock:
                self._fetch_failure_count += 1
                self._last_error_code = "long_quote_unavailable"
        with SessionGuard(request.session):
            completed_at = max(request.observed_at, _shanghai(self._now()))
            fresh = _fresh_quotes(features, frozenset(self._codes), completed_at, request.preview_context)
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
                    ("long_quotes", f"long-quotes:{request.observed_at:%Y%m%dT%H%M%S%f}"),
                    ("watchlist", self._watchlist_version),
                ),
                items=items,
            )
            expected = self._index.snapshot(Strategy.LONG).current
            published = self._index.publish(
                projection,
                expected_version=expected.version if expected is not None else None,
            )
            with SessionGuard(request.session), self._lock:
                self._live_count = live_count
                self._retained_count = retained_count
                self._missing_count = missing_count
                self._degraded_reasons = tuple(dict.fromkeys(degraded_reasons))
                if published.accepted:
                    self._retained_trade_date = completed_at.date()
                    self._retained_quotes = next_retained
                    self._last_observed_at = request.observed_at
                    self._last_generation = generation
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
                        selection_date=item.selected_on,
                        selection_price=item.selection_price,
                    )
                )
                continue
            if quote_status == "live":
                live_count += 1
            else:
                retained_count += 1
            available_status: Literal["live", "retained"] = "live" if quote_status == "live" else "retained"
            projection_items.append(
                _projection_item(
                    item,
                    self._groups[item.code],
                    quote,
                    available_status,
                    trade_date,
                )
            )
        return tuple(projection_items), retained, live_count, retained_count, missing_count


def _validate_items(items: tuple[LongWatchItemDefinition, ...]) -> tuple[LongWatchItemDefinition, ...]:
    normalized = tuple(items)
    codes = tuple(item.code for item in normalized)
    if len(codes) != len(set(codes)):
        raise ValueError("long runtime requires a unique fixed watchlist")
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
    preview_context: PreviewContext | None = None,
) -> dict[str, MarketQuote]:
    candidates: dict[str, list[MarketQuote]] = {}
    for feature in features:
        quote = feature.quote
        try:
            source_time = _shanghai(quote.source_time)
            received_time = _shanghai(quote.received_time)
        except ValueError:
            continue
        if (
            quote.code not in allowed_codes
            or source_time > observed_at
            or received_time > observed_at
            or (preview_context is not None and not preview_context.accepts_quote(source_time, observed_at))
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
    projection_date: date,
) -> LongProjectionItem:
    price = quote.price
    if price is None or price <= 0.0:
        raise ValueError("long projection quote requires a positive price")
    return LongProjectionItem(
        item.code,
        group,
        quote.data_version,
        name=item.name,
        industry=item.industry,
        price=price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        market_cap=quote.market_cap,
        source=quote.source,
        source_time=quote.source_time,
        quote_status=quote_status,
        selection_date=item.selected_on,
        selection_price=item.selection_price,
        selection_to_now_pct=(
            (price / item.selection_price - 1.0) * 100.0 if projection_date >= item.selected_on else None
        ),
    )


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _non_negative_or_none(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value >= 0.0 else None


def _shanghai(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("long quote time must be timezone-aware")
    return value.astimezone(SHANGHAI)


__all__ = ["LongRuntime", "LongRuntimeDependencies", "LongRuntimeStatus"]
