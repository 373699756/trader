"""Bounded current-industry reference adapter for production model inputs."""

from __future__ import annotations

import hashlib
import multiprocessing
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as daytime
from multiprocessing.connection import Connection
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.infra.cache_contracts import canonical_json_bytes
from trader.infra.market_data.observations import SourceObservation
from trader.recommendation.application.runtime.schedule import shanghai_now

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_EXPECTED_CLASSIFICATION = "证监会行业分类"


@dataclass(frozen=True)
class BaoStockIndustryRow:
    source_code: str
    industry: str
    classification: str
    update_date: str


@dataclass(frozen=True)
class BaoStockIndustryHealthStatus:
    planned_count: int = 0
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    snapshot_rows: int = 0
    invalid_rows: int = 0
    last_latency_ms: float = 0.0
    last_error: str | None = None
    last_source_time: datetime | None = None
    timeout_seconds: float = 0.0


class BaoStockIndustryFetch(Protocol):
    def __call__(self, trade_date: str, timeout_seconds: float) -> Sequence[BaoStockIndustryRow]: ...


class BaoStockIndustryClient:
    """Fetch one whole-market current snapshot without touching history archives."""

    def __init__(
        self,
        *,
        fetch_rows: BaoStockIndustryFetch | None = None,
        timeout_seconds: float = 120.0,
        minimum_rows: int = 4_000,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        if timeout_seconds <= 0.0 or minimum_rows < 1:
            raise ValueError("BaoStock industry timeout and minimum rows must be positive")
        self._fetch_rows = fetch_rows or _fetch_rows_in_bounded_process
        self._timeout_seconds = float(timeout_seconds)
        self._minimum_rows = minimum_rows
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._status = BaoStockIndustryHealthStatus(timeout_seconds=self._timeout_seconds)

    def fetch(self, observed_at: datetime) -> tuple[SourceObservation, ...]:
        local = shanghai_now(observed_at)
        started_at = self._monotonic()
        with self._lock:
            self._status = replace(self._status, planned_count=self._status.planned_count + 1)
        try:
            raw_rows = tuple(self._fetch_rows(local.date().isoformat(), self._timeout_seconds))
            selected, invalid_rows = _select_current_rows(raw_rows, local.date())
            if len(selected) < self._minimum_rows:
                raise ValueError("BaoStock current industry snapshot is incomplete")
            observations = _industry_observations(selected, observed_at)
            if not observations:
                raise RuntimeError("baostock_industry_snapshot_empty")
        except Exception as exc:
            elapsed_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
            with self._lock:
                current = self._status
                self._status = replace(
                    current,
                    error_count=current.error_count + 1,
                    timeout_count=current.timeout_count + int(isinstance(exc, TimeoutError)),
                    last_latency_ms=elapsed_ms,
                    last_error=type(exc).__name__,
                )
            raise
        elapsed_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
        with self._lock:
            current = self._status
            self._status = BaoStockIndustryHealthStatus(
                planned_count=current.planned_count,
                success_count=current.success_count + 1,
                error_count=current.error_count,
                timeout_count=current.timeout_count,
                snapshot_rows=len(observations),
                invalid_rows=invalid_rows,
                last_latency_ms=elapsed_ms,
                last_error=None,
                last_source_time=observed_at,
                timeout_seconds=self._timeout_seconds,
            )
        return observations

    def health(self) -> BaoStockIndustryHealthStatus:
        with self._lock:
            return self._status


def _select_current_rows(
    rows: Sequence[BaoStockIndustryRow],
    observed_date: date,
) -> tuple[tuple[BaoStockIndustryRow, ...], int]:
    selected: dict[str, BaoStockIndustryRow] = {}
    invalid = 0
    for row in rows:
        code = _normalize_source_code(row.source_code)
        effective_date = _parse_date(row.update_date)
        if (
            code is None
            or not row.industry.strip()
            or row.classification.strip() != _EXPECTED_CLASSIFICATION
            or effective_date is None
            or effective_date > observed_date
        ):
            invalid += 1
            continue
        normalized = BaoStockIndustryRow(
            code,
            row.industry.strip(),
            _EXPECTED_CLASSIFICATION,
            effective_date.isoformat(),
        )
        current = selected.get(code)
        if current is None or normalized.update_date > current.update_date:
            selected[code] = normalized
        elif normalized.update_date == current.update_date and normalized.industry != current.industry:
            raise ValueError("BaoStock industry rows conflict at the same effective date")
    return tuple(selected[code] for code in sorted(selected)), invalid


def _industry_observations(
    rows: tuple[BaoStockIndustryRow, ...],
    observed_at: datetime,
) -> tuple[SourceObservation, ...]:
    batch_material = tuple((row.source_code, row.industry, row.update_date) for row in rows)
    batch_hash = hashlib.sha256(canonical_json_bytes(batch_material)).hexdigest()
    data_version = f"baostock-industry:{shanghai_now(observed_at).date().isoformat()}:{batch_hash[:16]}"
    observations: list[SourceObservation] = []
    for row in rows:
        fields = {
            "model_industry": row.industry,
            "model_industry_classification": row.classification,
            "model_industry_data_version": data_version,
            "model_industry_effective_date": row.update_date,
            "model_industry_source": "baostock",
        }
        effective_at = datetime.combine(date.fromisoformat(row.update_date), daytime.min, _SHANGHAI)
        observations.append(
            SourceObservation(
                source="baostock_industry",
                subject_key=row.source_code,
                observed_at=observed_at,
                source_time=observed_at,
                received_at=observed_at,
                effective_at=effective_at,
                data_version=data_version,
                fields=fields,
                missing_reasons={},
                payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
                status="success",
                error_code=None,
            )
        )
    return tuple(observations)


def _normalize_source_code(value: str) -> str | None:
    normalized = value.strip().lower()
    if len(normalized) == 9 and normalized[2] == "." and normalized[:2] in {"sh", "sz", "bj"}:
        normalized = normalized[3:]
    return normalized if len(normalized) == 6 and normalized.isdigit() else None


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _fetch_rows_in_bounded_process(trade_date: str, timeout_seconds: float) -> tuple[BaoStockIndustryRow, ...]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_baostock_worker, args=(trade_date, sending), daemon=True)
    process.start()
    sending.close()
    try:
        if not receiving.poll(timeout_seconds):
            process.terminate()
            process.join(timeout=2.0)
            raise TimeoutError("BaoStock industry query exceeded its deadline")
        status, payload = receiving.recv()
    finally:
        receiving.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=2.0)
    if status != "success":
        raise RuntimeError(str(payload))
    return tuple(BaoStockIndustryRow(*item) for item in payload)


def _baostock_worker(trade_date: str, sender: Connection) -> None:
    try:
        import baostock

        login = baostock.login()
        if str(login.error_code) != "0":
            raise RuntimeError(f"supplier_login_rejected_{str(login.error_code)[:24]}")
        try:
            result = baostock.query_stock_industry(code="", date=trade_date)
            if str(result.error_code) != "0":
                raise RuntimeError(f"industry_query_rejected_{str(result.error_code)[:24]}")
            rows: list[tuple[str, str, str, str]] = []
            while result.next():
                values = dict(zip(result.fields, result.get_row_data(), strict=True))
                rows.append(
                    (
                        str(values.get("code", "")),
                        str(values.get("industry", "")),
                        str(values.get("industryClassification", "")),
                        str(values.get("updateDate", "")),
                    )
                )
        finally:
            baostock.logout()
        sender.send(("success", tuple(rows)))
    except BaseException as exc:
        sender.send(("failed", type(exc).__name__))
    finally:
        sender.close()


__all__ = [
    "BaoStockIndustryClient",
    "BaoStockIndustryHealthStatus",
    "BaoStockIndustryRow",
]
