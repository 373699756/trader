"""Tushare slow-reference loading, caching and structured fallback."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import TypeVar, cast
from zoneinfo import ZoneInfo

from trader.application.cache import CacheIdentity, build_cache_identity, canonical_json_bytes
from trader.application.schedule import shanghai_now
from trader.application.source_lanes import SourceRequestSuperseded
from trader.infra.market_data.gateway import MarketDataGateway
from trader.infra.market_data.history import DailyBar, PriceAdjustment
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_history import HistoryStore
from trader.infra.market_data.service_support import _normalize_codes, _source_batch_identity
from trader.infra.market_data.tushare import TushareClient

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAY_END = time(23, 59, 59)


class ReferenceLoader:
    def __init__(
        self,
        gateway: MarketDataGateway,
        history: HistoryStore,
        runner: MarketTaskRunner,
        client: TushareClient | None,
        *,
        monotonic: Callable[[], float],
    ) -> None:
        self._gateway = gateway
        self._history_store = history
        self._runner = runner
        self._client = client
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._reference_fields: dict[str, dict[str, float]] = {}
        self._reference_versions: dict[str, str] = {}
        self._reference_version_order: dict[str, tuple[datetime, datetime, str]] = {}

    def schedule_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        normalized = _normalize_codes(codes)
        lanes = self._runner.source_lanes
        if lanes is None:
            self.refresh_reference_data(normalized, observed_at, force=force)
            return
        if lanes.owns_current_thread("tushare"):
            self._refresh_tushare_reference_data(normalized, observed_at, force=force)
        else:
            tushare_identity = _source_batch_identity("reference_data", normalized, observed_at, force=force)
            tushare_future = lanes.submit(
                "tushare",
                tushare_identity,
                observed_at,
                self._refresh_tushare_reference_data,
                normalized,
                observed_at,
                force=force,
            )
            tushare_future.add_done_callback(_observe_reference_refresh)
        if not normalized:
            return
        if lanes.owns_current_thread("history"):
            self._history_store.load(normalized, force=force)
        else:
            history_identity = _source_batch_identity("daily_history", normalized, observed_at, force=force)
            history_future = lanes.submit(
                "history",
                history_identity,
                observed_at,
                self._history_store.load,
                normalized,
                force=force,
            )
            history_future.add_done_callback(_observe_reference_refresh)

    def refresh_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        normalized = _normalize_codes(codes)
        self._refresh_tushare_reference_data(normalized, observed_at, force=force)
        self._history_store.load(normalized, force=force)

    def _refresh_tushare_reference_data(
        self,
        normalized: Sequence[str],
        observed_at: datetime,
        *,
        force: bool,
    ) -> None:
        tushare_history: tuple[SourceObservation, ...] = ()
        if self._client is not None:
            if not self._client.supports("security_master"):
                if normalized and self._client.supports("forward_adjusted_daily"):
                    tushare_history = self.load_history_batch(normalized, observed_at, force=force)
                self.apply_history(tushare_history)
                return
            masters = self.load(
                "security_master_calendar",
                "security_master",
                {"dataset": "security_master", "market": "ashare"},
                observed_at,
                self._client.fetch_security_master,
                observed_at,
                force=force,
            )
            listing_dates = tuple(
                parsed
                for observation in masters
                if observation.status == "success"
                and isinstance(raw := observation.fields.get("listing_date"), str)
                and (parsed := _parse_date(raw)) is not None
            )
            calendars = (
                self.load(
                    "security_master_calendar",
                    "trading_calendar",
                    {
                        "dataset": "trading_calendar",
                        "start_date": min(listing_dates).isoformat(),
                        "end_date": shanghai_now(observed_at).date().isoformat(),
                    },
                    observed_at,
                    self._client.fetch_trading_calendar,
                    min(listing_dates),
                    shanghai_now(observed_at).date(),
                    observed_at,
                    force=force,
                )
                if listing_dates
                else ()
            )
            self._gateway.update_reference_observations((*calendars, *masters))
            if normalized:
                valuation_trade_date = _latest_effective_trade_date(calendars, observed_at)
                if self._client.supports("forward_adjusted_daily"):
                    tushare_history = self.load_history_batch(normalized, observed_at, force=force)
                valuation_observations = (
                    self.load(
                        "daily_valuation_financials",
                        "daily_valuation:" + ",".join(normalized),
                        {
                            "dataset": "daily_valuation",
                            "codes": normalized,
                            "trade_date": valuation_trade_date.isoformat(),
                        },
                        observed_at,
                        self._client.fetch_daily_valuations,
                        normalized,
                        valuation_trade_date,
                        observed_at,
                        force=force,
                    )
                    if valuation_trade_date is not None
                    else ()
                )
                financial_observations = self.load(
                    "daily_valuation_financials",
                    "financial_indicators:" + ",".join(normalized),
                    {"dataset": "financial_indicators", "codes": normalized},
                    observed_at,
                    self._client.fetch_financial_indicators,
                    normalized,
                    observed_at,
                    force=force,
                )
                self.apply_fields("valuation", valuation_observations)
                self.apply_fields("financial", financial_observations)
        self.apply_history(tushare_history)

    def load(
        self,
        dataset: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
        function: Callable[..., Sequence[SourceObservation]],
        /,
        *args: object,
        force: bool,
        **kwargs: object,
    ) -> tuple[SourceObservation, ...]:
        if self._client is None:
            return ()
        identity = build_cache_identity(
            dataset=dataset,
            source="tushare",
            subject_key=subject_key,
            request=request,
            trade_date=shanghai_now(observed_at).date().isoformat(),
            phase="all_day",
            source_contract_version=self._runner.source_contract_versions.get("tushare", "tushare-component-v1"),
            config_version=self._runner.config_version,
            schema_version=self._runner.schema_version,
        )
        cache = self._runner.cache

        def load() -> tuple[SourceObservation, ...]:
            lane_identity = _source_batch_identity(dataset, (subject_key,), observed_at, request=request, force=force)
            observations = tuple(
                self._runner.run_source_task("tushare", lane_identity, observed_at, function, *args, **kwargs)
            )
            completed_at = max(observed_at, self._runner.wall_clock())
            cacheable = tuple(
                item
                for item in observations
                if item.status == "success"
                and item.data_version.strip()
                and item.source_time <= completed_at
                and item.received_at <= completed_at
                and item.effective_at <= completed_at
            )
            if cache is not None:
                if cacheable:
                    cache.put(
                        identity,
                        cacheable,
                        data_version=max(item.data_version for item in cacheable),
                        source_time=max(item.source_time for item in cacheable),
                    )
                else:
                    error_code = next(
                        (item.error_code for item in observations if item.error_code),
                        "no_data",
                    )
                    cache.put_negative(identity, error_code=error_code)
            return cacheable

        if cache is not None and not force:
            lookup = cache.get(identity)
            if lookup is not None and lookup.state == "negative":
                return ()
            if lookup is not None and lookup.value is not None:
                observations = cast(tuple[SourceObservation, ...], lookup.value)
                if lookup.state != "fresh" and not lookup.retry_suppressed:
                    if self._runner.source_lanes is not None and self._runner.source_lanes.owns_current_thread(
                        "tushare"
                    ):
                        refreshed = cast(tuple[SourceObservation, ...], cache.coalesce(identity, load))
                        if refreshed:
                            return refreshed
                        refreshed_lookup = cache.get(identity)
                        reason = (
                            refreshed_lookup.error_code
                            if refreshed_lookup is not None and refreshed_lookup.error_code is not None
                            else "reference_refresh_failed"
                        )
                        return tuple(self._mark_reference_degraded(item, reason) for item in observations)
                    else:
                        self._schedule_tushare_refresh(
                            identity,
                            dataset,
                            subject_key,
                            request,
                            observed_at,
                            function,
                            args,
                            kwargs,
                        )
                if lookup.state != "fresh" or lookup.error_code is not None:
                    observations = tuple(
                        self._mark_reference_degraded(item, lookup.error_code or "reference_data_degraded")
                        for item in observations
                    )
                return observations

        if cache is None:
            return load()
        loaded = cast(tuple[SourceObservation, ...], cache.coalesce(identity, load))
        if loaded:
            return loaded
        fallback = cache.get(identity)
        if fallback is None or fallback.value is None:
            return loaded
        reason = fallback.error_code or "reference_refresh_failed"
        return tuple(
            self._mark_reference_degraded(item, reason) for item in cast(tuple[SourceObservation, ...], fallback.value)
        )

    def _schedule_tushare_refresh(
        self,
        identity: CacheIdentity,
        dataset: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
        function: Callable[..., Sequence[SourceObservation]],
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> None:
        lanes = self._runner.source_lanes
        if lanes is None:
            return
        refresh_identity = "tushare-refresh:" + hashlib.sha256(canonical_json_bytes(identity.as_dict())).hexdigest()

        def refresh() -> tuple[SourceObservation, ...]:
            return self.load(
                dataset,
                subject_key,
                request,
                observed_at,
                function,
                *args,
                force=True,
                **kwargs,
            )

        lanes.submit("tushare", refresh_identity, observed_at, refresh)

    def apply_history(self, observations: Sequence[SourceObservation]) -> None:
        grouped: dict[str, list[DailyBar]] = {}
        applied_observations: list[SourceObservation] = []
        for observation in observations:
            if observation.fields.get("reference_data_degraded") is True:
                continue
            bar = _tushare_daily_bar(observation)
            if bar is None or bar.adjustment is not PriceAdjustment.QFQ:
                continue
            grouped.setdefault(observation.subject_key, []).append(bar)
            applied_observations.append(observation)
        if not grouped:
            return
        self._history_store.apply_source_bars(grouped, source="tushare")
        with self._lock:
            self._record_tushare_version_locked("daily_history", applied_observations)

    def apply_fields(
        self,
        namespace: str,
        observations: Sequence[SourceObservation],
    ) -> None:
        latest: dict[str, SourceObservation] = {}
        for observation in observations:
            current = latest.get(observation.subject_key)
            if current is None or (
                observation.effective_at,
                observation.received_at,
                observation.data_version,
                observation.payload_hash,
            ) > (
                current.effective_at,
                current.received_at,
                current.data_version,
                current.payload_hash,
            ):
                latest[observation.subject_key] = observation
        if not latest:
            return
        with self._lock:
            for code, observation in latest.items():
                if len(code) != 6 or not code.isdigit():
                    continue
                fields = self._reference_fields.setdefault(code, {})
                for name, value in observation.fields.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        fields[f"tushare_{namespace}_{name}"] = float(value)
            self._record_tushare_version_locked(namespace, tuple(latest.values()))

    def _record_tushare_version_locked(
        self,
        namespace: str,
        observations: Sequence[SourceObservation],
    ) -> None:
        if not observations:
            return
        latest = max(observations, key=lambda item: (item.source_time, item.received_at, item.data_version))
        order = (latest.source_time, latest.received_at, latest.data_version)
        current = self._reference_version_order.get(namespace)
        if current is None or order > current:
            self._reference_version_order[namespace] = order
            self._reference_versions[namespace] = latest.data_version

    def load_history_batch(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool,
    ) -> tuple[SourceObservation, ...]:
        client = self._client
        normalized = _normalize_codes(codes)
        if client is None or not normalized:
            return ()
        trade_date = shanghai_now(observed_at).date()
        start_date = trade_date - timedelta(days=120)
        forward_adjusted = client.supports("forward_adjusted_daily")
        dataset = "forward_adjusted_daily" if forward_adjusted else "daily_history"
        adjust = "qfq" if forward_adjusted else "none"
        loader = client.fetch_forward_adjusted_daily if forward_adjusted else client.fetch_daily_history
        return self.load(
            "daily_history",
            ",".join(normalized),
            {
                "dataset": dataset,
                "codes": normalized,
                "start_date": start_date.isoformat(),
                "end_date": trade_date.isoformat(),
                "adjust": adjust,
            },
            observed_at,
            loader,
            normalized,
            start_date,
            trade_date,
            observed_at,
            force=force,
        )

    def fields(self, codes: Sequence[str]) -> Mapping[str, Mapping[str, float]]:
        selected = set(codes)
        with self._lock:
            return {code: dict(values) for code, values in self._reference_fields.items() if code in selected}

    def versions(self) -> Mapping[str, str]:
        with self._lock:
            return dict(self._reference_versions)

    def health(self) -> Mapping[str, object]:
        return dict(self._client.health()) if self._client is not None else {}

    @staticmethod
    def _mark_reference_degraded(observation: SourceObservation, reason: str) -> SourceObservation:
        fields = dict(observation.fields)
        fields["reference_data_degraded"] = True
        if "board" in fields:
            fields["board_reliability"] = "degraded"
        payload_hash = hashlib.sha256(canonical_json_bytes(fields)).hexdigest()
        return replace(
            observation,
            fields=fields,
            missing_reasons={**dict(observation.missing_reasons), "cache_refresh": reason},
            payload_hash=payload_hash,
        )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _latest_effective_trade_date(
    observations: Sequence[SourceObservation],
    observed_at: datetime,
) -> date | None:
    local = shanghai_now(observed_at)
    available: list[date] = []
    for observation in observations:
        if observation.status != "success" or observation.fields.get("is_open") is not True:
            continue
        raw = observation.fields.get("calendar_date")
        parsed = _parse_date(raw) if isinstance(raw, str) else None
        if parsed is None:
            continue
        effective_at = datetime.combine(parsed, _DAY_END, _SHANGHAI)
        if effective_at <= local:
            available.append(parsed)
    return max(available, default=None)


def _tushare_daily_bar(observation: SourceObservation) -> DailyBar | None:
    fields = observation.fields
    trade_date_value = fields.get("trade_date")
    if not isinstance(trade_date_value, str):
        return None
    numbers = {
        name: _finite_number(fields.get(source_name))
        for name, source_name in {
            "open_price": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "volume": "vol",
            "amount": "amount",
            "pct_change": "pct_chg",
        }.items()
    }
    required = ("open_price", "close", "high", "low", "volume", "amount", "pct_change")
    if any(numbers[name] is None for name in required):
        return None
    try:
        parsed_date = date.fromisoformat(trade_date_value.replace("/", "-"))
    except ValueError:
        compact = trade_date_value.replace("-", "")
        if len(compact) != 8 or not compact.isdigit():
            return None
        parsed_date = datetime.strptime(compact, "%Y%m%d").date()
    return DailyBar(
        trade_date=parsed_date.isoformat(),
        open_price=cast(float, numbers["open_price"]),
        close=cast(float, numbers["close"]),
        high=cast(float, numbers["high"]),
        low=cast(float, numbers["low"]),
        volume=cast(float, numbers["volume"]) * 100.0,
        amount=cast(float, numbers["amount"]) * 1000.0,
        pct_change=cast(float, numbers["pct_change"]),
        turnover_rate=_finite_number(fields.get("turnover_rate")),
        adjustment=(
            PriceAdjustment.QFQ if fields.get("price_adjustment") == PriceAdjustment.QFQ.value else PriceAdjustment.RAW
        ),
        source="tushare",
    )


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _observe_reference_refresh(future: Future[_T]) -> None:
    try:
        future.result()
    except SourceRequestSuperseded:
        return
    except Exception as exc:
        _LOGGER.warning("reference data refresh failed: %s", type(exc).__name__)


__all__ = ["ReferenceLoader"]
