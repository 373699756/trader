"""Bounded recommendation pipeline and deterministic single-tick use case."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from trader.application.candidate_features import fetch_strategy_features
from trader.application.events import BoundedEventQueue, EventPriority, event_from_audit_record, new_event
from trader.application.ports import (
    DeepSeekReviewPort,
    EventAuditPort,
    MarketDataPort,
    MarketDataUnavailable,
    SnapshotRepositoryPort,
    TradingCalendarPort,
)
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import MarketPhase, decision_at, freeze_due_at, shanghai_now, trade_date_at
from trader.application.status import RuntimeState
from trader.domain.models import FeatureSnapshot, FilterAudit, LiveOverlay, LiveQuote, RecommendationSnapshot, Strategy

_LOGGER = logging.getLogger(__name__)


class RecommendationPipeline:
    def __init__(
        self,
        market_data: MarketDataPort,
        calendar: TradingCalendarPort,
        reviews: DeepSeekReviewPort | None,
        repository: SnapshotRepositoryPort,
        event_audit: EventAuditPort,
        publisher: SnapshotPublisher,
        engine: RecommendationEngine,
        state: RuntimeState,
        *,
        config_version: str,
        candidate_pool_size: int,
        event_queue_size: int,
        priority_queue_size: int,
        now: Callable[[], datetime],
        long_codes: Sequence[str] = (),
        long_target_prices: Mapping[str, float | None] | None = None,
    ) -> None:
        self._market_data = market_data
        self._calendar = calendar
        self._reviews = reviews
        self._repository = repository
        self._event_audit = event_audit
        self._publisher = publisher
        self._engine = engine
        self._state = state
        self._config_version = config_version
        self._candidate_pool_size = candidate_pool_size
        self._now = now
        self._long_codes = tuple(long_codes)
        self._long_target_prices = dict(long_target_prices or {})
        self._queue = BoundedEventQueue(
            maximum_size=event_queue_size,
            reserved_priority_size=priority_queue_size,
        )
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._candidate_codes: tuple[str, ...] = ()
        self._candidate_features: tuple[FeatureSnapshot, ...] = ()
        self._market_features: tuple[FeatureSnapshot, ...] = ()
        self._filter_reasons: Mapping[str, int] = {}
        self._filter_details: tuple[FilterAudit, ...] = ()
        self._filtered_count = 0
        self._frozen_keys: set[tuple[Strategy, str]] = set()
        self._live_overlays: dict[tuple[Strategy, str], LiveOverlay] = {}

    def initialize(self) -> Mapping[str, int]:
        self._repository.initialize()
        recovery = self._repository.recover()
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            for trade_date in self._repository.recommendation_dates(strategy):
                key = (strategy, trade_date)
                self._frozen_keys.add(key)
                self._state.restore_frozen(strategy, trade_date)
        for strategy in Strategy:
            latest = self._repository.latest(strategy)
            if latest is not None:
                self._state.restore_snapshot(latest)
                if latest.frozen:
                    overlay = self._repository.load_live_overlay(strategy, latest.trade_date)
                    if overlay is not None and overlay.snapshot_id == latest.snapshot_id:
                        self._live_overlays[(strategy, latest.trade_date)] = overlay
        now = self._now()
        trade_day = trade_date_at(now)
        catchup = self._freeze_available_snapshots(
            now,
            freeze_due_at(now, is_trading_day=self._calendar.is_trading_day(trade_day)),
        )
        for record in self._event_audit.pending_priority_events():
            try:
                event = event_from_audit_record(record)
            except (KeyError, TypeError, ValueError) as exc:
                self._state.record_error(f"cannot replay persisted priority event: {exc}")
                continue
            if self._queue.put(event):
                self._state.increment("events_replayed")
        return {**recovery, "catchup_frozen": len(catchup)}

    def start(self) -> bool:
        if self._worker is not None and self._worker.is_alive():
            return False
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="trader-pipeline", daemon=False)
        self._worker.start()
        self._state.mark_started(True)
        return True

    def stop(self, timeout_seconds: float = 15.0) -> None:
        self._stop_event.set()
        self._queue.close()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, timeout_seconds))
        self._state.mark_started(False)

    def submit_tick(self, at: datetime | None = None) -> bool:
        now = at or self._now()
        trade_day = trade_date_at(now)
        is_trading_day = self._calendar.is_trading_day(trade_day)
        decision = decision_at(now, is_trading_day=is_trading_day)
        self._state.record_tick(decision.phase.value, now)
        if decision.phase is MarketPhase.CLOSED:
            return False
        is_freeze = bool(decision.freeze_strategies)
        event = new_event(
            "freeze" if is_freeze else "schedule_tick",
            subject_key="market",
            trade_date=trade_day.isoformat(),
            phase=decision.phase.value,
            strategy=None,
            priority=EventPriority.FREEZE if is_freeze else EventPriority.MARKET_QUOTES,
            data_version=f"tick:{shanghai_now(now).strftime('%H%M%S')}",
            config_version=self._config_version,
            created_at=now,
            payload={"freeze_strategies": list(decision.freeze_strategies)},
        )
        if is_freeze:
            self._event_audit.append_event(event.audit_record(status="pending"))
        accepted = self._queue.put(event)
        if accepted:
            self._state.increment("events_submitted")
        elif is_freeze:
            self._event_audit.append_event(event.audit_record(status="failed", error="priority_queue_full"))
        return accepted

    def run_once(self, at: datetime) -> tuple[RecommendationSnapshot, ...]:
        trade_day = trade_date_at(at)
        is_trading_day = self._calendar.is_trading_day(trade_day)
        decision = decision_at(at, is_trading_day=is_trading_day)
        self._state.record_tick(decision.phase.value, at)
        if decision.phase is MarketPhase.CLOSED:
            return ()
        return self._process_schedule(at, decision.phase, decision.freeze_strategies)

    def status(self) -> dict[str, object]:
        market_data = dict(self._market_data.health())
        market_data["topk_quote_age"] = _topk_quote_age(self._state, self._live_overlays, self._now())
        dependencies = {
            "market_data": market_data,
            "deepseek": dict(self._reviews.status()) if self._reviews is not None else {"enabled": False},
            "event_queue": self._queue.status(),
            "publisher": self._publisher.status(),
        }
        return self._state.snapshot(dependencies)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            event = self._queue.get(timeout_seconds=0.5)
            if event is None:
                continue
            try:
                self._event_audit.append_event(event.audit_record(status="running"))
                freeze_raw = event.payload.get("freeze_strategies")
                freezes = tuple(str(value) for value in freeze_raw) if isinstance(freeze_raw, list) else ()
                self._process_schedule(event.created_at, MarketPhase(event.phase), freezes)
                self._event_audit.append_event(event.audit_record(status="success"))
                self._state.increment("events_completed")
            except Exception as exc:
                _LOGGER.exception("pipeline event failed", extra={"event_id": event.event_id})
                self._event_audit.append_event(event.audit_record(status="failed", error=str(exc)))
                self._state.increment("events_failed")
                self._state.record_error(str(exc))

    def _process_schedule(
        self,
        now: datetime,
        phase: MarketPhase,
        freeze_strategies: Sequence[str],
    ) -> tuple[RecommendationSnapshot, ...]:
        snapshots: list[RecommendationSnapshot] = []
        trade_date = trade_date_at(now).isoformat()
        if phase in {
            MarketPhase.WARMUP,
            MarketPhase.TODAY_OBSERVE,
            MarketPhase.TODAY_MAIN,
            MarketPhase.TODAY_LATE,
            MarketPhase.AFTERNOON,
            MarketPhase.FINAL_REVIEW,
            MarketPhase.FINAL_QUOTE,
        }:
            self._refresh_candidates(now, phase)
        if phase is MarketPhase.WARMUP and self._reviews is not None and self._candidate_features:
            self._reviews.preheat(
                self._candidate_features,
                phase=phase.value,
                deadline=shanghai_now(now).replace(hour=9, minute=30, second=0, microsecond=0),
            )

        for strategy in _strategies_for_phase(phase):
            if (strategy, trade_date) in self._frozen_keys or self._state.is_frozen(strategy, trade_date):
                continue
            snapshot = self._score_strategy(strategy, now, phase, trade_date)
            if snapshot is not None:
                snapshots.append(snapshot)

        snapshots.extend(self._freeze_available_snapshots(now, freeze_strategies))
        if phase in {MarketPhase.FROZEN, MarketPhase.AFTER_CLOSE}:
            self._refresh_live_overlays(now, phase)
        return tuple(snapshots)

    def _freeze_available_snapshots(
        self,
        now: datetime,
        freeze_strategies: Sequence[str],
    ) -> tuple[RecommendationSnapshot, ...]:
        snapshots: list[RecommendationSnapshot] = []
        trade_date = trade_date_at(now).isoformat()
        for raw_strategy in freeze_strategies:
            strategy = Strategy(raw_strategy)
            key = (strategy, trade_date)
            if key in self._frozen_keys or self._state.is_frozen(strategy, trade_date):
                continue
            current = self._state.latest(strategy)
            if current is None or current.trade_date != trade_date:
                self._state.record_error(f"{strategy.value} freeze unavailable: no current pre-cutoff snapshot")
                continue
            boundary = _freeze_boundary(now, strategy)
            if current.published_at > boundary:
                self._state.record_error(f"{strategy.value} freeze unavailable: latest snapshot is after cutoff")
                continue
            if (boundary - current.published_at).total_seconds() > 30:
                self._state.record_error(f"{strategy.value} freeze unavailable: latest snapshot is stale at cutoff")
                continue
            maximum_age = 20.0 if strategy is Strategy.TODAY else 30.0
            anchors: dict[str, object] = {}
            invalid_quotes: list[str] = []
            for recommendation in current.recommendations:
                quote = recommendation.features.quote
                age = (boundary - quote.source_time).total_seconds()
                anchors[quote.code] = {
                    "source": quote.source,
                    "source_time": quote.source_time.isoformat(),
                    "age_seconds": round(age, 3),
                }
                if age < 0.0 or age > maximum_age:
                    invalid_quotes.append(f"{quote.code}:{age:.3f}")
            if invalid_quotes:
                self._state.record_error(
                    f"{strategy.value} freeze unavailable: quote age outside 0-{maximum_age:.0f}s at cutoff "
                    + ",".join(invalid_quotes)
                )
                continue
            frozen = replace(
                current,
                frozen=True,
                published_at=boundary,
                config_version=self._config_version,
                metadata={**current.metadata, "freeze_anchor": anchors},
            )
            self._repository.freeze(frozen)
            self._state.mark_frozen(frozen)
            self._publisher.publish(frozen)
            self._frozen_keys.add(key)
            snapshots.append(frozen)
        return tuple(snapshots)

    def _refresh_live_overlays(self, now: datetime, phase: MarketPhase) -> None:
        trade_date = trade_date_at(now).isoformat()
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            snapshot = self._state.latest(strategy)
            if snapshot is None or not snapshot.frozen or snapshot.trade_date != trade_date:
                continue
            key = (strategy, trade_date)
            existing = self._live_overlays.get(key)
            if existing is None:
                existing = self._repository.load_live_overlay(strategy, trade_date)
            if existing is not None and existing.snapshot_id != snapshot.snapshot_id:
                existing = None
            if existing is not None and existing.closing:
                self._live_overlays[key] = existing
                continue
            codes = tuple(item.features.quote.code for item in snapshot.recommendations)
            if not codes:
                continue
            try:
                features = tuple(self._market_data.fetch_candidate_features(codes, now))
            except MarketDataUnavailable as exc:
                self._state.record_error(f"{strategy.value} live overlay degraded: {str(exc)[:500]}")
                continue
            quotes = dict(existing.quotes) if existing is not None else {}
            allowed = set(codes)
            updated_codes: set[str] = set()
            for feature in features:
                quote = feature.quote
                if quote.code not in allowed or quote.source_time > now or quote.price is None or quote.price <= 0:
                    continue
                quotes[quote.code] = LiveQuote(
                    code=quote.code,
                    price=quote.price,
                    pct_change=quote.pct_change,
                    source=quote.source,
                    source_time=quote.source_time,
                    received_time=quote.received_time,
                    data_version=quote.data_version,
                )
                updated_codes.add(quote.code)
            if not updated_codes:
                continue
            overlay = LiveOverlay(
                snapshot_id=snapshot.snapshot_id,
                strategy=strategy,
                trade_date=trade_date,
                version=_overlay_version(snapshot.snapshot_id, now, quotes),
                observed_at=now,
                quotes=quotes,
                closing=phase is MarketPhase.AFTER_CLOSE and updated_codes == allowed,
            )
            if not self._repository.save_live_overlay(overlay):
                persisted = self._repository.load_live_overlay(strategy, trade_date)
                if persisted is not None and persisted.snapshot_id == snapshot.snapshot_id:
                    self._live_overlays[key] = persisted
                continue
            self._live_overlays[key] = overlay
            self._publisher.publish_overlay(overlay)

    def _refresh_candidates(self, now: datetime, phase: MarketPhase) -> None:
        try:
            market_features = tuple(self._market_data.fetch_market_features(now))
        except MarketDataUnavailable as exc:
            reason = str(exc)[:500]
            _LOGGER.warning("candidate refresh degraded during %s: %s", phase.value, reason)
            self._state.increment("market_refresh_failures")
            self._state.record_error(f"market data degraded during {phase.value}: {reason}")
            return

        maximum_age = _maximum_age_seconds(phase)
        candidates, reasons, details = self._engine.preselect(
            market_features,
            now=now,
            max_age_seconds=maximum_age,
            limit=self._candidate_pool_size,
        )
        self._market_features = market_features
        self._candidate_codes = tuple(feature.quote.code for feature in candidates)
        self._candidate_features = candidates
        self._filter_reasons = reasons
        self._filter_details = details
        self._filtered_count = len({item.stock_code for item in details})

    def _score_strategy(
        self,
        strategy: Strategy,
        now: datetime,
        phase: MarketPhase,
        trade_date: str,
    ) -> RecommendationSnapshot | None:
        codes = self._long_codes if strategy is Strategy.LONG else self._candidate_codes
        if not codes:
            return None
        features, data_version = fetch_strategy_features(self._market_data, strategy, codes, now)
        if not features:
            return None
        deadline = _review_deadline(now, phase)
        review_port = self._reviews if phase not in {MarketPhase.DEEPSEEK_CUTOFF, MarketPhase.FINAL_QUOTE} else None
        is_long = strategy is Strategy.LONG
        snapshot = self._engine.build_snapshot(
            strategy,
            features,
            now=now,
            phase=phase.value,
            trade_date=trade_date,
            data_version=data_version,
            review_port=review_port,
            review_deadline=deadline,
            max_age_seconds=_maximum_age_seconds(phase, strategy),
            filtered_count=0 if is_long else self._filtered_count,
            filter_reasons={} if is_long else self._filter_reasons,
            filter_details=() if is_long else self._filter_details,
            target_prices=self._long_target_prices if strategy is Strategy.LONG else None,
            market_features=self._market_features,
            requested_codes=codes,
            preselect_max_age_seconds=_maximum_age_seconds(phase),
            candidate_pool_size=self._candidate_pool_size,
        )
        snapshot = replace(snapshot, config_version=self._config_version)
        self._repository.publish(snapshot)
        self._state.publish(snapshot)
        self._publisher.publish(snapshot)
        return snapshot


def _strategies_for_phase(phase: MarketPhase) -> tuple[Strategy, ...]:
    if phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
        return (Strategy.TODAY,)
    if phase in {MarketPhase.AFTERNOON, MarketPhase.FINAL_REVIEW, MarketPhase.FINAL_QUOTE}:
        return (Strategy.TOMORROW, Strategy.D25, Strategy.LONG)
    return ()


def _maximum_age_seconds(phase: MarketPhase, strategy: Strategy | None = None) -> float:
    if strategy is Strategy.TODAY or phase in {
        MarketPhase.TODAY_OBSERVE,
        MarketPhase.TODAY_MAIN,
        MarketPhase.TODAY_LATE,
    }:
        return 20.0
    return 30.0


def _review_deadline(now: datetime, phase: MarketPhase) -> datetime:
    local = shanghai_now(now)
    if phase in {MarketPhase.TODAY_OBSERVE, MarketPhase.TODAY_MAIN, MarketPhase.TODAY_LATE}:
        return local.replace(hour=11, minute=20, second=0, microsecond=0)
    return local.replace(hour=14, minute=48, second=0, microsecond=0)


def _freeze_boundary(now: datetime, strategy: Strategy) -> datetime:
    local = shanghai_now(now)
    if strategy is Strategy.TODAY:
        return local.replace(hour=11, minute=20, second=0, microsecond=0)
    return local.replace(hour=14, minute=50, second=0, microsecond=0)


def _topk_quote_age(
    state: RuntimeState,
    overlays: Mapping[tuple[Strategy, str], LiveOverlay],
    now: datetime,
) -> Mapping[str, object]:
    per_strategy: dict[str, object] = {}
    active_ages: list[float] = []
    excluded_frozen: list[str] = []
    for strategy in Strategy:
        snapshot = state.latest(strategy)
        if snapshot is None:
            continue
        if snapshot.frozen:
            overlay = overlays.get((strategy, snapshot.trade_date))
            if overlay is None or overlay.snapshot_id != snapshot.snapshot_id:
                excluded_frozen.append(strategy.value)
                continue
            ages = [quote.age_seconds(now) for quote in overlay.quotes.values()]
        else:
            ages = [item.features.quote.age_seconds(now) for item in snapshot.recommendations]
        active_ages.extend(ages)
        per_strategy[strategy.value] = _age_summary(ages)
    return {
        "target_seconds": 10.0,
        **_age_summary(active_ages),
        "per_strategy": per_strategy,
        "excluded_frozen_strategies": sorted(excluded_frozen),
        "measured_at": now.isoformat(),
    }


def _overlay_version(snapshot_id: str, observed_at: datetime, quotes: Mapping[str, LiveQuote]) -> str:
    values = [snapshot_id, observed_at.isoformat()]
    for code, quote in sorted(quotes.items()):
        values.extend(
            (code, quote.data_version, quote.source_time.isoformat(), str(quote.price), str(quote.pct_change))
        )
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:24]


def _age_summary(ages: Sequence[float]) -> dict[str, object]:
    if not ages:
        return {"sample_count": 0, "p95_seconds": None, "maximum_seconds": None, "meets_target": None}
    ordered = sorted(max(0.0, float(age)) for age in ages)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    p95 = round(ordered[p95_index], 3)
    return {
        "sample_count": len(ordered),
        "p95_seconds": p95,
        "maximum_seconds": round(ordered[-1], 3),
        "meets_target": p95 <= 10.0,
    }


__all__ = ["RecommendationPipeline"]
