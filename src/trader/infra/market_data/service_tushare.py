"""Tushare slow-reference loading, caching and structured fallback."""

from __future__ import annotations

import hashlib
import logging
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Protocol, TypeVar, cast
from zoneinfo import ZoneInfo

from trader.application.cache import CacheIdentity, CacheIdentitySpec, build_cache_identity, canonical_json_bytes
from trader.application.ports.data_plane import (
    DataPlaneRecoverySummary,
    DataPlaneUnavailableError,
    SecurityMasterRecord,
    SourceCursorRecord,
)
from trader.application.ports.types import JsonObject, JsonValue
from trader.application.schedule import shanghai_now
from trader.application.source_lanes import SourceRequestSupersededError
from trader.infra.market_data.gateway import MarketDataGateway
from trader.infra.market_data.history import DailyBar, PriceAdjustment
from trader.infra.market_data.market_cache_identity import _normalize_codes, _source_batch_identity
from trader.infra.market_data.observations import JsonScalar, SourceObservation
from trader.infra.market_data.service_calendar_state import (
    calendar_observations_from_record as _calendar_observations_from_record,
)
from trader.infra.market_data.service_calendar_state import calendar_sessions_payload as _calendar_sessions_payload
from trader.infra.market_data.service_calendar_state import parse_date as _parse_date
from trader.infra.market_data.service_calendar_state import (
    trading_calendar_cursor_from_observations as _trading_calendar_cursor_from_observations,
)
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_history import HistoryCache
from trader.infra.market_data.tushare import TushareClient

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAY_END = time(23, 59, 59)
_TUSHARE_SOURCE = "tushare"
_TRADING_CALENDAR_CURSOR_NAME = "tushare.trading_calendar"


class _ReferenceDataPlane(Protocol):
    def recover(self) -> DataPlaneRecoverySummary: ...

    def save_security_master_recent(self, record: SecurityMasterRecord) -> None: ...

    def save_source_cursor_recent(self, record: SourceCursorRecord) -> None: ...

    def load_security_master_recent_records(
        self, codes: Sequence[str] | None = None
    ) -> tuple[SecurityMasterRecord, ...]: ...

    def load_source_cursor_recent_records(
        self,
        cursor_names: Sequence[str] | None = None,
    ) -> tuple[SourceCursorRecord, ...]: ...


@dataclass(frozen=True)
class ReferenceLoadRequest:
    dataset: str
    subject_key: str
    request: Mapping[str, object]
    options: _ReferenceLoadOptions
    force: bool = False

    def __post_init__(self) -> None:
        if self.force != self.options.force:
            object.__setattr__(self, "options", replace(self.options, force=self.force))


@dataclass(frozen=True)
class _ReferenceLoadOptions:
    observed_at: datetime
    function: Callable[..., Sequence[SourceObservation]]
    args: tuple[object, ...]
    force: bool
    kwargs: Mapping[str, object]


class ReferenceLoader:
    def __init__(  # noqa: PLR0913
        self,
        gateway: MarketDataGateway,
        history: HistoryCache,
        runner: MarketTaskRunner,
        client: TushareClient | None,
        *,
        data_plane: _ReferenceDataPlane | None = None,
        monotonic: Callable[[], float],
    ) -> None:
        self._gateway = gateway
        self._history_cache = history
        self._runner = runner
        self._client = client
        self._data_plane = data_plane
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._reference_fields: dict[str, dict[str, float]] = {}
        self._reference_versions: dict[str, str] = {}
        self._reference_version_order: dict[str, tuple[datetime, datetime, str]] = {}
        self._trading_calendar_cursor: str | None = None
        self._trading_calendar_observations: dict[str, SourceObservation] = {}

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
            self._history_cache.load(normalized, force=force)
        else:
            history_identity = _source_batch_identity("daily_history", normalized, observed_at, force=force)
            history_future = lanes.submit(
                "history",
                history_identity,
                observed_at,
                self._history_cache.load,
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
        self._history_cache.load(normalized, force=force)

    def _refresh_tushare_reference_data(
        self,
        normalized: Sequence[str],
        observed_at: datetime,
        *,
        force: bool,
    ) -> None:
        masters: tuple[SourceObservation, ...] = ()
        calendars: tuple[SourceObservation, ...] = ()
        tushare_history: tuple[SourceObservation, ...] = ()
        calendar_start_date = observed_at.date()
        if self._client is not None:
            if not self._client.supports("security_master"):
                if normalized and self._client.supports("forward_adjusted_daily"):
                    tushare_history = self.load_history_batch(normalized, observed_at, force=force)
                self.apply_history(tushare_history)
                return
            masters = self.load(
                ReferenceLoadRequest(
                    "security_master_calendar",
                    "security_master",
                    {"dataset": "security_master", "market": "ashare"},
                    _ReferenceLoadOptions(
                        observed_at=observed_at,
                        function=self._client.fetch_security_master,
                        args=(observed_at,),
                        force=force,
                        kwargs={},
                    ),
                )
            )
            listing_dates = tuple(
                parsed
                for observation in masters
                if observation.status == "success"
                and isinstance(raw := observation.fields.get("listing_date"), str)
                and (parsed := _parse_date(raw)) is not None
            )
            if listing_dates:
                calendar_start_date = self._next_calendar_start(min(listing_dates))
            calendars = (
                self.load(
                    ReferenceLoadRequest(
                        "security_master_calendar",
                        "trading_calendar",
                        {
                            "dataset": "trading_calendar",
                            "start_date": calendar_start_date.isoformat(),
                            "end_date": shanghai_now(observed_at).date().isoformat(),
                        },
                        _ReferenceLoadOptions(
                            observed_at=observed_at,
                            function=self._client.fetch_trading_calendar,
                            args=(min(listing_dates), shanghai_now(observed_at).date(), observed_at),
                            force=force,
                            kwargs={},
                        ),
                    )
                )
                if listing_dates
                else ()
            )
            self._gateway.update_reference_observations((*calendars, *masters))
            valuation_observations: tuple[SourceObservation, ...] = ()
            financial_observations: tuple[SourceObservation, ...] = ()
            if normalized:
                valuation_trade_date = _latest_effective_trade_date(calendars, observed_at)
                if self._client.supports("forward_adjusted_daily"):
                    tushare_history = self.load_history_batch(normalized, observed_at, force=force)
                valuation_observations = (
                    self.load(
                        ReferenceLoadRequest(
                            "daily_valuation_financials",
                            "daily_valuation:" + ",".join(normalized),
                            {
                                "dataset": "daily_valuation",
                                "codes": normalized,
                                "trade_date": valuation_trade_date.isoformat(),
                            },
                            _ReferenceLoadOptions(
                                observed_at=observed_at,
                                function=self._client.fetch_daily_valuations,
                                args=(normalized, valuation_trade_date, observed_at),
                                force=force,
                                kwargs={},
                            ),
                        )
                    )
                    if valuation_trade_date is not None
                    else ()
                )
                financial_observations = self.load(
                    ReferenceLoadRequest(
                        "daily_valuation_financials",
                        "financial_indicators:" + ",".join(normalized),
                        {"dataset": "financial_indicators", "codes": normalized},
                        _ReferenceLoadOptions(
                            observed_at=observed_at,
                            function=self._client.fetch_financial_indicators,
                            args=(normalized, observed_at),
                            force=force,
                            kwargs={},
                        ),
                    )
                )
            self.apply_fields("valuation", valuation_observations)
            self.apply_fields("financial", financial_observations)
        self.apply_history(tushare_history)
        self._persist_reference_data(observed_at, masters=masters, calendars=calendars)

    def recover(self) -> DataPlaneRecoverySummary:
        if self._data_plane is None:
            return DataPlaneRecoverySummary()
        try:
            summary = self._data_plane.recover()
        except DataPlaneUnavailableError:
            _LOGGER.warning("reference data plane unavailable during recovery")
            return DataPlaneRecoverySummary()
        try:
            self._restore_from_data_plane()
        except DataPlaneUnavailableError:
            _LOGGER.warning("reference data plane unavailable when restoring")
            return DataPlaneRecoverySummary()
        except Exception as exc:
            _LOGGER.warning("reference data recovery failed: %s", type(exc).__name__)
            return DataPlaneRecoverySummary()
        return summary

    def _restore_from_data_plane(self) -> None:
        if self._data_plane is None:
            return
        masters = self._data_plane.load_security_master_recent_records()
        if masters:
            self._gateway.update_reference_observations(
                tuple(self._to_reference_observation(record) for record in masters)
            )
        cursors = self._data_plane.load_source_cursor_recent_records(cursor_names=(_TRADING_CALENDAR_CURSOR_NAME,))
        if cursors:
            calendar_record = cursors[-1]
            self._trading_calendar_cursor = calendar_record.cursor_value
            restored_calendars = _calendar_observations_from_record(calendar_record)
            if restored_calendars:
                with self._lock:
                    self._trading_calendar_observations = {
                        observation.subject_key: observation for observation in restored_calendars
                    }
                self._gateway.update_reference_observations(restored_calendars)

    def _next_calendar_start(self, listing_min: date) -> date:
        cursor = self._trading_calendar_cursor
        if cursor is None:
            return listing_min
        parsed = _parse_date(cursor)
        if parsed is None:
            self._trading_calendar_cursor = None
            return listing_min
        return max(listing_min, parsed)

    def _persist_reference_data(
        self,
        observed_at: datetime,
        *,
        masters: tuple[SourceObservation, ...],
        calendars: tuple[SourceObservation, ...],
    ) -> None:
        if self._data_plane is None:
            return
        self._persist_security_masters(observed_at, masters)
        self._persist_trading_calendar(observed_at, calendars)

    def _persist_security_masters(
        self,
        observed_at: datetime,
        masters: Sequence[SourceObservation],
    ) -> None:
        if self._data_plane is None:
            return
        for master in masters:
            if master.status != "success":
                continue
            try:
                self._data_plane.save_security_master_recent(
                    SecurityMasterRecord(
                        code=master.subject_key,
                        observed_at=observed_at,
                        source_time=master.source_time,
                        source=_TUSHARE_SOURCE,
                        data_version=master.data_version,
                        payload=dict(master.fields),
                    )
                )
            except DataPlaneUnavailableError:
                _LOGGER.warning("security master persistence unavailable")
            except Exception:
                _LOGGER.exception("security master persistence failed")

    def _persist_trading_calendar(
        self,
        observed_at: datetime,
        calendars: Sequence[SourceObservation],
    ) -> None:
        if self._data_plane is None:
            return
        if _trading_calendar_cursor_from_observations(calendars) is None:
            return
        try:
            calendar_snapshot = self._merge_calendar_observations(calendars)
            cursor_value = calendar_snapshot[-1].subject_key
            latest = max(calendar_snapshot, key=lambda obs: (obs.source_time, obs.received_at, obs.data_version))
            self._data_plane.save_source_cursor_recent(
                SourceCursorRecord(
                    cursor_name=_TRADING_CALENDAR_CURSOR_NAME,
                    cursor_value=cursor_value,
                    observed_at=observed_at,
                    source_time=latest.source_time,
                    source=_TUSHARE_SOURCE,
                    data_version=latest.data_version,
                    payload={
                        "start_date": calendar_snapshot[0].subject_key,
                        "end_date": calendar_snapshot[-1].subject_key,
                        "count": len(calendar_snapshot),
                        "sessions": _calendar_sessions_payload(calendar_snapshot),
                    },
                )
            )
            self._trading_calendar_cursor = cursor_value
        except DataPlaneUnavailableError:
            _LOGGER.warning("trading calendar cursor persistence unavailable")
        except Exception:
            _LOGGER.exception("trading calendar cursor persistence failed")

    def _merge_calendar_observations(
        self,
        calendars: Sequence[SourceObservation],
    ) -> tuple[SourceObservation, ...]:
        with self._lock:
            for observation in calendars:
                if observation.status == "success" and _parse_date(observation.subject_key) is not None:
                    self._trading_calendar_observations[observation.subject_key] = observation
            return tuple(
                self._trading_calendar_observations[key] for key in sorted(self._trading_calendar_observations)
            )

    def _to_reference_observation(self, record: SecurityMasterRecord) -> SourceObservation:
        fields = _source_fields_for_observation(record.payload)
        return SourceObservation(
            source=record.source,
            subject_key=record.code,
            observed_at=record.observed_at,
            source_time=record.source_time,
            received_at=record.observed_at,
            effective_at=record.observed_at,
            data_version=record.data_version,
            fields=fields,
            missing_reasons={},
            payload_hash=record.payload_hash or hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
            status="success",
            error_code=None,
        )

    def load(
        self,
        request: ReferenceLoadRequest,
    ) -> tuple[SourceObservation, ...]:
        if self._client is None:
            return ()
        identity = build_cache_identity(
            CacheIdentitySpec(
                dataset=request.dataset,
                source="tushare",
                subject_key=request.subject_key,
                request=request.request,
                trade_date=shanghai_now(request.options.observed_at).date().isoformat(),
                phase="all_day",
                source_contract_version=self._runner.source_contract_versions.get("tushare", "tushare-component-v1"),
                config_version=self._runner.config_version,
                schema_version=self._runner.schema_version,
            )
        )
        cache = self._runner.cache

        def load() -> tuple[SourceObservation, ...]:
            lane_identity = _source_batch_identity(
                request.dataset,
                (request.subject_key,),
                request.options.observed_at,
                request=request.request,
                force=request.options.force,
            )
            observations = tuple(
                self._runner.run_source_task(
                    "tushare",
                    lane_identity,
                    request.options.observed_at,
                    request.options.function,
                    *request.options.args,
                    **request.options.kwargs,
                )
            )
            completed_at = max(request.options.observed_at, self._runner.wall_clock())
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

        if cache is not None and not request.options.force:
            cached = self._cached_reference(identity, request, load)
            if cached is not None:
                return cached

        if cache is None:
            return load()
        loaded = cast(tuple[SourceObservation, ...], cache.coalesce(identity, load))
        if loaded:
            return loaded
        return self._reference_fallback(identity, loaded)

    def _cached_reference(
        self,
        identity: CacheIdentity,
        request: ReferenceLoadRequest,
        load: Callable[[], tuple[SourceObservation, ...]],
    ) -> tuple[SourceObservation, ...] | None:
        cache = self._runner.cache
        assert cache is not None
        lookup = cache.get(identity)
        if lookup is None:
            return None
        if lookup.state == "negative":
            return ()
        if lookup.value is None:
            return None
        observations = cast(tuple[SourceObservation, ...], lookup.value)
        if lookup.state != "fresh" and not lookup.retry_suppressed:
            lanes = self._runner.source_lanes
            if lanes is not None and lanes.owns_current_thread("tushare"):
                refreshed = cast(tuple[SourceObservation, ...], cache.coalesce(identity, load))
                if refreshed:
                    return refreshed
                return self._reference_fallback(identity, observations)
            self._schedule_tushare_refresh(identity, request)
        if lookup.state != "fresh" or lookup.error_code is not None:
            reason = lookup.error_code or "reference_data_degraded"
            observations = tuple(self._mark_reference_degraded(item, reason) for item in observations)
        return observations

    def _reference_fallback(
        self,
        identity: CacheIdentity,
        default: tuple[SourceObservation, ...],
    ) -> tuple[SourceObservation, ...]:
        cache = self._runner.cache
        assert cache is not None
        fallback = cache.get(identity)
        if fallback is None or fallback.value is None:
            return default
        reason = fallback.error_code or "reference_refresh_failed"
        return tuple(
            self._mark_reference_degraded(item, reason) for item in cast(tuple[SourceObservation, ...], fallback.value)
        )

    def _schedule_tushare_refresh(
        self,
        identity: CacheIdentity,
        request: ReferenceLoadRequest,
    ) -> None:
        lanes = self._runner.source_lanes
        if lanes is None:
            return
        refresh_identity = "tushare-refresh:" + hashlib.sha256(canonical_json_bytes(identity.as_dict())).hexdigest()

        def refresh() -> tuple[SourceObservation, ...]:
            return self.load(
                replace(request, options=replace(request.options, force=True)),
            )

        lanes.submit("tushare", refresh_identity, request.options.observed_at, refresh)

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
        self._history_cache.apply_source_bars(grouped, source="tushare")
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
            ReferenceLoadRequest(
                "daily_history",
                ",".join(normalized),
                {
                    "dataset": dataset,
                    "codes": normalized,
                    "start_date": start_date.isoformat(),
                    "end_date": trade_date.isoformat(),
                    "adjust": adjust,
                },
                _ReferenceLoadOptions(
                    observed_at=observed_at,
                    function=loader,
                    args=(normalized, start_date, trade_date, observed_at),
                    force=force,
                    kwargs={},
                ),
            )
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


def _to_json_object(
    value: Mapping[str, JsonValue] | Mapping[str, int | float | bool | str | None] | object,
) -> JsonObject:
    if isinstance(value, Mapping):
        return cast(JsonObject, dict(value))
    raise TypeError("payload must be a mapping")


def _source_fields_for_observation(payload: Mapping[str, JsonValue]) -> dict[str, JsonScalar]:
    fields: dict[str, JsonScalar] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            fields[key] = value
            continue
        if value is None or isinstance(value, bool):
            fields[key] = value
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            fields[key] = float(value)
            continue
        if isinstance(value, float):
            fields[key] = value if math.isfinite(value) else math.nan
            continue
        fields[key] = str(value)
    return fields


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
    except SourceRequestSupersededError:
        return
    except Exception as exc:
        _LOGGER.warning("reference data refresh failed: %s", type(exc).__name__)


__all__ = ["ReferenceLoader"]
