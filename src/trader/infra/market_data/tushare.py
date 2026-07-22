"""Optional Tushare SDK adapter for slow, structured reference data only."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Protocol

from trader.application.cache import canonical_json_bytes
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.tushare_support import (
    _calendar_observation,
    _calendar_ranges,
    _data_version,
    _default_sdk_factory,
    _error_code,
    _failed_observation,
    _generic_observation,
    _invoke,
    _percentile,
    _records,
    _require_aware,
    _security_master_observation,
    _ts_code,
)


class _SdkFactory(Protocol):
    def __call__(self, token: str, timeout_seconds: float) -> object: ...


class _SourceStopped(RuntimeError):
    """Internal cooperative cancellation signal for a Tushare batch."""


class TushareClient:
    """Lazily creates the optional SDK and never serves high-frequency quotes."""

    def __init__(
        self,
        *,
        token: str,
        points: int = 2000,
        timeout_seconds: float,
        circuit_breaker_failures: int = 3,
        circuit_breaker_seconds: float = 60.0,
        sdk_factory: _SdkFactory | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if timeout_seconds != 8.0:
            raise ValueError("Tushare transport timeout must be exactly 8 seconds")
        self._token = token.strip()
        self._points = max(0, points)
        self._timeout_seconds = timeout_seconds
        self._failure_limit = max(1, circuit_breaker_failures)
        self._breaker_seconds = max(0.1, circuit_breaker_seconds)
        self._sdk_factory = sdk_factory or _default_sdk_factory
        self._cancel_requested = cancel_requested
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._client: object | None = None
        self._planned_count = 0
        self._success_count = 0
        self._error_count = 0
        self._consecutive_failures = 0
        self._timeout_count = 0
        self._last_latency_ms = 0.0
        self._latencies_ms: deque[float] = deque(maxlen=256)
        self._last_source_time: datetime | None = None
        self._degraded_reason = "missing_token" if not self._token else ""
        self._open_until = 0.0
        self._half_open_probe = False

    def fetch_security_master(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        if not self.supports("security_master"):
            return self._insufficient_points("security_master", observed_at)
        return self._fetch_records(
            "security_master",
            observed_at,
            "stock_basic",
            {
                "exchange": "",
                "list_status": "L",
                "fields": "ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date",
            },
            _security_master_observation,
        )

    def fetch_trading_calendar(
        self,
        start_date: date,
        end_date: date,
        observed_at: datetime,
    ) -> tuple[SourceObservation, ...]:
        if not self.supports("trading_calendar"):
            return self._insufficient_points("trading_calendar", observed_at)
        if end_date < start_date:
            raise ValueError("Tushare calendar end_date cannot precede start_date")
        ranges = _calendar_ranges(start_date, end_date)
        observations: list[SourceObservation] = []
        for range_start, range_end in ranges:
            chunk = self._fetch_records(
                "trading_calendar",
                observed_at,
                "trade_cal",
                {
                    "exchange": "SSE",
                    "start_date": range_start.strftime("%Y%m%d"),
                    "end_date": range_end.strftime("%Y%m%d"),
                    "fields": "exchange,cal_date,is_open,pretrade_date",
                },
                _calendar_observation,
            )
            observations.extend(chunk)
            if any(item.status == "failed" for item in chunk):
                break
        return tuple(observations)

    def fetch_forward_adjusted_daily(
        self,
        codes: Sequence[str],
        start_date: date,
        end_date: date,
        observed_at: datetime,
    ) -> tuple[SourceObservation, ...]:
        if not self.supports("forward_adjusted_daily"):
            return self._insufficient_points("forward_adjusted_daily", observed_at)
        return _with_price_adjustment(
            self._fetch_per_code(
                "forward_adjusted_daily",
                codes,
                observed_at,
                lambda client, code: _invoke(
                    client,
                    "pro_bar",
                    ts_code=_ts_code(code),
                    start_date=start_date.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                    adj="qfq",
                    freq="D",
                ),
            ),
            "qfq",
        )

    def fetch_daily_history(
        self,
        codes: Sequence[str],
        start_date: date,
        end_date: date,
        observed_at: datetime,
    ) -> tuple[SourceObservation, ...]:
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized:
            return ()
        return _with_price_adjustment(
            self._fetch_records(
                "daily_history",
                observed_at,
                "daily",
                {
                    "ts_code": ",".join(_ts_code(code) for code in normalized),
                    "start_date": start_date.strftime("%Y%m%d"),
                    "end_date": end_date.strftime("%Y%m%d"),
                    "fields": "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
                },
                lambda row, observed, received, version: _generic_observation(
                    "daily_history",
                    row,
                    observed,
                    received,
                    version,
                ),
                empty_is_error=True,
            ),
            "raw",
        )

    def fetch_daily_valuations(
        self,
        codes: Sequence[str],
        trade_date: date,
        observed_at: datetime,
    ) -> tuple[SourceObservation, ...]:
        if not self.supports("daily_valuation"):
            return self._insufficient_points("daily_valuation", observed_at)
        return self._fetch_per_code(
            "daily_valuation",
            codes,
            observed_at,
            lambda client, code: _invoke(
                client,
                "daily_basic",
                ts_code=_ts_code(code),
                trade_date=trade_date.strftime("%Y%m%d"),
                fields="ts_code,trade_date,close,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv",
            ),
        )

    def fetch_financial_indicators(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> tuple[SourceObservation, ...]:
        if not self.supports("financial_indicators"):
            return self._insufficient_points("financial_indicators", observed_at)
        return self._fetch_per_code(
            "financial_indicators",
            codes,
            observed_at,
            lambda client, code: _invoke(
                client,
                "fina_indicator",
                ts_code=_ts_code(code),
                fields="ts_code,ann_date,end_date,eps,dt_eps,roe,roe_dt,assets_turn,debt_to_assets",
            ),
        )

    def supports(self, dataset: str) -> bool:
        minimum_points = {
            "daily_history": 120,
            "security_master": 2000,
            "trading_calendar": 2000,
            "forward_adjusted_daily": 2000,
            "daily_valuation": 2000,
            "financial_indicators": 2000,
        }
        required = minimum_points.get(dataset)
        return required is not None and self._points >= required

    def history_mode(self) -> str:
        return "forward_adjusted" if self.supports("forward_adjusted_daily") else "unadjusted_daily"

    def health(self) -> Mapping[str, object]:
        measured_at = self._wall_clock()
        with self._lock:
            return {
                "enabled": bool(self._token),
                "access_points": self._points,
                "history_mode": self.history_mode(),
                "planned_count": self._planned_count,
                "success_count": self._success_count,
                "error_count": self._error_count,
                "consecutive_failures": self._consecutive_failures,
                "circuit_open": self._open_until > self._monotonic(),
                "timeout_count": self._timeout_count,
                "last_latency_ms": round(self._last_latency_ms, 2),
                "p50_latency_ms": _percentile(self._latencies_ms, 0.50),
                "p95_latency_ms": _percentile(self._latencies_ms, 0.95),
                "degraded_reason": self._degraded_reason or None,
                "timeout_seconds": self._timeout_seconds,
                "data_age_seconds": max(0.0, (measured_at - self._last_source_time).total_seconds())
                if self._last_source_time is not None
                else None,
            }

    def _fetch_records(
        self,
        dataset: str,
        observed_at: datetime,
        method: str,
        arguments: Mapping[str, object],
        converter: Callable[[Mapping[str, object], datetime, datetime, str], SourceObservation | None],
        *,
        empty_is_error: bool = False,
    ) -> tuple[SourceObservation, ...]:
        _require_aware(observed_at)
        started = self._begin()
        if started is None:
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "circuit_open"),)
        if self._cancel_requested():
            self._record_stopped(started)
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "stopped"),)
        if not self._token:
            self._record_error(started, "missing_token")
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "missing_token"),)
        try:
            client = self._client_instance()
            if self._cancel_requested():
                raise _SourceStopped
            records = _records(_invoke(client, method, **dict(arguments)))
            if self._cancel_requested():
                raise _SourceStopped
            received_at = max(observed_at, self._wall_clock())
            data_version = _data_version(dataset, records)
            observations = tuple(
                observation
                for row in records
                if (observation := converter(row, observed_at, received_at, data_version)) is not None
            )
        except _SourceStopped:
            self._record_stopped(started)
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "stopped"),)
        except Exception as exc:
            error_code = _error_code(exc)
            self._record_error(started, error_code)
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), error_code),)
        if observations:
            self._record_success(started, received_at)
            return observations
        if empty_is_error:
            self._record_error(started, "no_data")
        else:
            self._record_success(started, received_at)
        return (
            SourceObservation(
                source="tushare",
                subject_key=dataset,
                observed_at=observed_at,
                source_time=received_at,
                received_at=received_at,
                effective_at=received_at,
                data_version=data_version,
                fields={},
                missing_reasons={dataset: "source_returned_no_rows"},
                payload_hash=hashlib.sha256(canonical_json_bytes([])).hexdigest(),
                status="no_data",
                error_code="no_data",
            ),
        )

    def _insufficient_points(self, dataset: str, observed_at: datetime) -> tuple[SourceObservation, ...]:
        _require_aware(observed_at)
        return (
            _failed_observation(
                dataset,
                observed_at,
                max(observed_at, self._wall_clock()),
                "insufficient_points",
            ),
        )

    def _fetch_per_code(
        self,
        dataset: str,
        codes: Sequence[str],
        observed_at: datetime,
        fetcher: Callable[[object, str], object],
    ) -> tuple[SourceObservation, ...]:
        _require_aware(observed_at)
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized:
            return ()
        started = self._begin()
        if started is None:
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "circuit_open"),)
        if self._cancel_requested():
            self._record_stopped(started)
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "stopped"),)
        if not self._token:
            self._record_error(started, "missing_token")
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "missing_token"),)
        try:
            client = self._client_instance()
            rows: list[Mapping[str, object]] = []
            failures: list[SourceObservation] = []
            failure_codes: list[str] = []
            for code in normalized:
                if self._cancel_requested():
                    raise _SourceStopped
                try:
                    rows.extend(_records(fetcher(client, code)))
                except Exception as exc:
                    error_code = _error_code(exc)
                    failure_codes.append(error_code)
                    failures.append(
                        _failed_observation(
                            dataset,
                            observed_at,
                            max(observed_at, self._wall_clock()),
                            error_code,
                            subject_key=code,
                        )
                    )
            if self._cancel_requested():
                raise _SourceStopped
            received_at = max(observed_at, self._wall_clock())
            version = _data_version(dataset, rows)
            observations = tuple(_generic_observation(dataset, row, observed_at, received_at, version) for row in rows)
        except _SourceStopped:
            self._record_stopped(started)
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), "stopped"),)
        except Exception as exc:
            error_code = _error_code(exc)
            self._record_error(started, error_code)
            return (_failed_observation(dataset, observed_at, max(observed_at, self._wall_clock()), error_code),)
        if failure_codes and not observations:
            self._record_error(started, failure_codes[0])
            return tuple(failures)
        if failure_codes:
            self._record_partial_success(started, received_at, failure_codes)
        else:
            self._record_success(started, received_at)
        return (*observations, *failures)

    def _client_instance(self) -> object:
        with self._lock:
            if self._client is None:
                self._client = self._sdk_factory(self._token, self._timeout_seconds)
            return self._client

    def _begin(self) -> float | None:
        with self._lock:
            self._planned_count += 1
            now = self._monotonic()
            if self._open_until > now or self._half_open_probe:
                self._degraded_reason = "circuit_open"
                return None
            if self._open_until > 0.0:
                self._half_open_probe = True
            return now

    def _record_success(self, started: float, source_time: datetime) -> None:
        with self._lock:
            self._success_count += 1
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_probe = False
            self._last_latency_ms = (self._monotonic() - started) * 1000.0
            self._latencies_ms.append(self._last_latency_ms)
            self._degraded_reason = ""
            self._last_source_time = source_time

    def _record_error(self, started: float, error_code: str) -> None:
        with self._lock:
            self._error_count += 1
            self._consecutive_failures += 1
            self._half_open_probe = False
            self._last_latency_ms = (self._monotonic() - started) * 1000.0
            self._latencies_ms.append(self._last_latency_ms)
            self._degraded_reason = error_code
            if self._consecutive_failures >= self._failure_limit:
                self._open_until = self._monotonic() + self._breaker_seconds
            if error_code == "timeout":
                self._timeout_count += 1

    def _record_partial_success(
        self,
        started: float,
        source_time: datetime,
        error_codes: Sequence[str],
    ) -> None:
        with self._lock:
            self._success_count += 1
            self._error_count += 1
            self._consecutive_failures = 0
            self._open_until = 0.0
            self._half_open_probe = False
            self._last_latency_ms = (self._monotonic() - started) * 1000.0
            self._latencies_ms.append(self._last_latency_ms)
            self._degraded_reason = "partial_batch"
            self._last_source_time = source_time
            if "timeout" in error_codes:
                self._timeout_count += 1

    def _record_stopped(self, started: float) -> None:
        with self._lock:
            self._half_open_probe = False
            self._last_latency_ms = (self._monotonic() - started) * 1000.0
            self._latencies_ms.append(self._last_latency_ms)
            self._degraded_reason = "stopped"


def _with_price_adjustment(
    observations: Sequence[SourceObservation],
    adjustment: str,
) -> tuple[SourceObservation, ...]:
    tagged: list[SourceObservation] = []
    for observation in observations:
        if observation.status != "success":
            tagged.append(observation)
            continue
        fields = {**dict(observation.fields), "price_adjustment": adjustment}
        tagged.append(
            replace(
                observation,
                fields=fields,
                payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
            )
        )
    return tuple(tagged)


__all__ = ["TushareClient"]
