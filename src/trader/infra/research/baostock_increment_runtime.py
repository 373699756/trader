"""Explicit BaoStock increment acquisition over an immutable parent archive."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnContext
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from trader.application.research.baostock_history_runtime import (
    BaoStockRuntimeProgress,
    BaoStockRuntimeProgressPort,
    BaoStockRuntimeRequest,
    BaoStockRuntimeStatus,
)
from trader.domain.research.baostock_active_archive import (
    BAOSTOCK_ARCHIVE_FIELD_FAMILIES,
    BaoStockActiveArchiveContext,
    BaoStockArchiveFieldFamily,
    BaoStockArchiveRecordKey,
    BaoStockFieldCoverage,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.infra.research.baostock_active_archive import (
    BaoStockActiveArchive,
    BaoStockActiveArchiveConflictError,
    BaoStockIncrementRecord,
    BaoStockIncrementWriter,
)
from trader.infra.research.baostock_archive_plan import ArchiveIncrementPlan, StockArchivePlan, build_archive_plan
from trader.infra.research.baostock_gateway import BaoStockRowResult, qfq_source_windows
from trader.infra.research.baostock_history_messages import SupplierCallActivity
from trader.infra.research.baostock_history_runtime import (
    _dependency_versions,
    _failure_code,
    _load_sdk,
    _login,
    _logout,
    _RateLimitedBaoStockSdk,
)
from trader.infra.research.baostock_partition_archive import BaoStockDailyPartitionedArchive

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
_FACT_FIELDS = "date,code,tradestatus,isST"


class BaoStockIncrementSourcePort(Protocol):
    def fetch_daily(
        self,
        code: str,
        family: BaoStockArchiveFieldFamily,
        dates: tuple[date, ...],
    ) -> BaoStockIncrementFetch: ...

    def fetch_industry(
        self,
        codes: frozenset[str],
        *,
        as_of: date,
    ) -> tuple[BaoStockIncrementRecord, ...]: ...


@dataclass(frozen=True)
class _IncrementWorkerResponse:
    status: BaoStockRuntimeStatus | None
    failure_reason: str = ""


@dataclass(frozen=True)
class _PreparedIncrement:
    plan: ArchiveIncrementPlan
    context: BaoStockActiveArchiveContext


@dataclass(frozen=True)
class _IncrementRun:
    request: BaoStockRuntimeRequest
    root: Path
    cancel_requested: Callable[[], bool]
    progress: BaoStockRuntimeProgressPort | None


@dataclass(frozen=True)
class _MonitorUpdate:
    result: tuple[BaoStockRuntimeStatus | None, str] | None
    prepared: _PreparedIncrement | None
    supplier_deadline: float | None


@dataclass(frozen=True)
class IncrementApplyOptions:
    sessions: int
    retries: int = 2
    cancel_requested: Callable[[], bool] = lambda: False
    report: Callable[[BaoStockRuntimeProgress], None] = lambda _value: None


@dataclass(frozen=True)
class BaoStockIncrementFetch:
    records: tuple[BaoStockIncrementRecord, ...]
    unavailable_keys: tuple[BaoStockArchiveRecordKey, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        unavailable = tuple(sorted(self.unavailable_keys))
        if bool(unavailable) != (self.unavailable_reason is not None):
            raise ValueError("BaoStock increment unavailable result is invalid")
        if self.unavailable_reason not in {None, "supplier_adjustment_unavailable"}:
            raise ValueError("BaoStock increment unavailable reason is invalid")
        if len(set(unavailable)) != len(unavailable):
            raise ValueError("BaoStock increment unavailable keys are duplicated")
        object.__setattr__(self, "unavailable_keys", unavailable)


def run_baostock_increment_update(
    request: BaoStockRuntimeRequest,
    root: Path,
    *,
    cancel_requested: Callable[[], bool],
    progress: BaoStockRuntimeProgressPort | None,
) -> BaoStockRuntimeStatus:
    """Run update in an isolated process so every supplier call has a hard timeout."""
    if not (root / "manifest.json").is_file():
        return BaoStockRuntimeStatus(
            state="failed",
            sessions=request.sessions,
            failure_reasons=("parent_manifest_unavailable",),
        )
    BaoStockDailyPartitionedArchive(root).verify()
    process_context = get_context("spawn")
    run = _IncrementRun(request, root, cancel_requested, progress)
    last_failure = "increment_worker_failed"
    prepared: _PreparedIncrement | None = None
    for _attempt in range(request.retries + 1):
        status, last_failure, prepared = _run_increment_attempt(
            process_context,
            run,
            prepared,
        )
        if status is not None:
            return status
    return BaoStockRuntimeStatus(
        state="failed",
        sessions=request.sessions,
        failure_reasons=(last_failure,),
    )


def _run_increment_attempt(
    process_context: SpawnContext,
    run: _IncrementRun,
    prepared: _PreparedIncrement | None,
) -> tuple[BaoStockRuntimeStatus | None, str, _PreparedIncrement | None]:
    parent, child = process_context.Pipe()
    process = cast(
        BaseProcess,
        process_context.Process(
            target=_increment_worker_main,
            args=(child, run.request, run.root.as_posix(), prepared),
        ),
    )
    process.start()
    child.close()
    try:
        return _monitor_increment_process(parent, process, run, prepared)
    except (EOFError, OSError):
        return None, "increment_worker_failed", prepared
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)


def _monitor_increment_process(
    connection: Connection,
    process: BaseProcess,
    run: _IncrementRun,
    prepared: _PreparedIncrement | None,
) -> tuple[BaoStockRuntimeStatus | None, str, _PreparedIncrement | None]:
    supplier_deadline: float | None = time.monotonic() + run.request.timeout_seconds
    while True:
        if run.cancel_requested():
            return _cancelled_status(run.request), "cancel_requested", prepared
        if supplier_deadline is not None and time.monotonic() >= supplier_deadline:
            return None, "supplier_call_timeout", prepared
        if not connection.poll(0.25):
            if not process.is_alive():
                return None, "increment_worker_failed", prepared
            continue
        update = _handle_worker_message(
            connection.recv(),
            prepared,
            run.progress,
            run.request.timeout_seconds,
        )
        prepared = update.prepared
        supplier_deadline = update.supplier_deadline
        if update.result is not None:
            return *update.result, prepared


def _handle_worker_message(
    message: object,
    prepared: _PreparedIncrement | None,
    progress: BaoStockRuntimeProgressPort | None,
    timeout_seconds: float,
) -> _MonitorUpdate:
    if isinstance(message, SupplierCallActivity):
        deadline = time.monotonic() + timeout_seconds if message.state == "started" else None
        return _MonitorUpdate(None, prepared, deadline)
    if isinstance(message, _PreparedIncrement):
        return _MonitorUpdate(None, message, None)
    if isinstance(message, BaoStockRuntimeProgress):
        if progress is not None:
            progress.publish(message)
        return _MonitorUpdate(None, prepared, None)
    if isinstance(message, _IncrementWorkerResponse):
        return _MonitorUpdate(
            (message.status, message.failure_reason or "increment_worker_failed"),
            prepared,
            None,
        )
    return _MonitorUpdate((None, "increment_worker_protocol_invalid"), prepared, None)


def _cancelled_status(request: BaoStockRuntimeRequest) -> BaoStockRuntimeStatus:
    return BaoStockRuntimeStatus(
        state="cancelled",
        sessions=request.sessions,
        failure_reasons=("cancel_requested",),
    )


def apply_increment_plan(
    plan: ArchiveIncrementPlan,
    archive: BaoStockActiveArchive,
    writer: BaoStockIncrementWriter,
    source: BaoStockIncrementSourcePort,
    options: IncrementApplyOptions,
) -> BaoStockRuntimeStatus:
    """Consume only plan gaps, retaining failed checkpoints for an idempotent retry."""
    return _PlanExecutor(plan, archive, writer, source, options).run()


class _PlanExecutor:
    def __init__(
        self,
        plan: ArchiveIncrementPlan,
        archive: BaoStockActiveArchive,
        writer: BaoStockIncrementWriter,
        source: BaoStockIncrementSourcePort,
        options: IncrementApplyOptions,
    ) -> None:
        self._plan = plan
        self._archive = archive
        self._writer = writer
        self._source = source
        self._options = options
        self._required_by_code: dict[str, set[BaoStockArchiveRecordKey]] = {}
        self._failed_codes: set[str] = set()
        self._failure_reasons: set[str] = set()
        self._completed_codes = 0
        self._fatal = False
        self._expected_records = sum(len(_required_keys(item)) for item in plan.stocks)
        self._downloaded_records = min(
            self._expected_records,
            sum(writer.completed_count(family) for family in ("daily_raw", "daily_qfq", "is_st")),
        )

    def run(self) -> BaoStockRuntimeStatus:
        for stock in self._plan.stocks:
            if self._options.cancel_requested():
                return self._cancelled()
            self._apply_stock(stock)
            if self._fatal:
                return self._fatal_status()
            self._report(stock.code)
        self._apply_industry()
        incomplete = self._incomplete_codes()
        if incomplete and not self._only_deterministic_gaps(incomplete):
            return self._incomplete_status(incomplete)
        coverage = _field_coverage(self._plan, self._writer)
        active = self._archive.publish(self._writer, coverage)
        if incomplete or self._failure_reasons:
            return self._incomplete_status(incomplete, manifest_hash=active.content_hash)
        return BaoStockRuntimeStatus(
            state="completed",
            sessions=self._options.sessions,
            shard_count=self._plan.partition_count,
            universe_count=self._plan.universe_count,
            completed_codes=self._plan.universe_count,
            manifest_hash=active.content_hash,
            failure_reasons=tuple(sorted(self._failure_reasons)),
        )

    def _apply_stock(self, stock: StockArchivePlan) -> None:
        required = _required_keys(stock)
        self._required_by_code[stock.code] = required
        for family, dates in _planned_daily_requests(stock, self._writer):
            if dates:
                self._fetch_family(stock.code, family, dates)
            if self._fatal:
                return
        if all(self._writer.completed(key) for key in required):
            self._completed_codes += 1
            self._failed_codes.discard(stock.code)

    def _fetch_family(
        self,
        code: str,
        family: BaoStockArchiveFieldFamily,
        dates: tuple[date, ...],
    ) -> None:
        keys = tuple(BaoStockArchiveRecordKey(code, day, family) for day in dates)
        for attempt in range(self._options.retries + 1):
            try:
                result = self._source.fetch_daily(code, family, dates)
                _save_fetch_response(self._writer, keys, result, family=family)
                self._downloaded_records = min(
                    self._expected_records,
                    self._downloaded_records + len(result.records),
                )
                if result.unavailable_reason is not None:
                    self._failure_reasons.add(result.unavailable_reason)
                    self._failed_codes.add(code)
                return
            except BaoStockActiveArchiveConflictError:
                raise
            except Exception as exc:  # supplier adapter exceptions become bounded checkpoints
                reason = _bounded_failure_code(exc)
                if reason != "supplier_query_failed_blacklisted" and attempt < self._options.retries:
                    continue
                self._record_failure(code, keys, reason)
                return

    def _record_failure(
        self,
        code: str,
        keys: tuple[BaoStockArchiveRecordKey, ...],
        reason: str,
    ) -> None:
        self._failure_reasons.add(reason)
        self._failed_codes.add(code)
        for key in keys:
            self._writer.record_failure(key, reason)
        self._fatal = reason == "supplier_query_failed_blacklisted"

    def _apply_industry(self) -> None:
        codes = frozenset(item.code for item in self._plan.stocks if item.missing_industry_dates)
        if not codes:
            return
        try:
            for record in self._source.fetch_industry(codes, as_of=self._plan.target_source_cutoff):
                self._writer.save(record)
        except BaoStockActiveArchiveConflictError:
            raise
        except Exception as exc:
            self._failure_reasons.add(_bounded_failure_code(exc))

    def _report(self, code: str) -> None:
        self._options.report(
            BaoStockRuntimeProgress(
                "downloading",
                current_code=code,
                sessions=self._options.sessions,
                universe_count=self._plan.universe_count,
                completed_codes=self._completed_codes,
                failed_codes=len(self._failed_codes),
                expected_records=self._expected_records,
                downloaded_records=self._downloaded_records,
                active_workers=1,
                last_failure_reason=next(iter(sorted(self._failure_reasons)), ""),
            )
        )

    def _incomplete_codes(self) -> set[str]:
        return {
            code
            for code, keys in self._required_by_code.items()
            if any(not self._writer.completed(key) for key in keys)
        }

    def _only_deterministic_gaps(self, incomplete: set[str]) -> bool:
        return all(
            self._writer.checkpoint(key).error_code == "supplier_adjustment_unavailable"
            for code in incomplete
            for key in self._required_by_code[code]
            if not self._writer.completed(key)
        )

    def _cancelled(self) -> BaoStockRuntimeStatus:
        return BaoStockRuntimeStatus(
            state="cancelled",
            sessions=self._options.sessions,
            universe_count=self._plan.universe_count,
            completed_codes=self._completed_codes,
            failed_codes=len(self._failed_codes),
            failure_reasons=("cancel_requested",),
        )

    def _fatal_status(self) -> BaoStockRuntimeStatus:
        return BaoStockRuntimeStatus(
            state="failed",
            sessions=self._options.sessions,
            universe_count=self._plan.universe_count,
            completed_codes=self._completed_codes,
            failed_codes=len(self._failed_codes),
            failure_reasons=("supplier_query_failed_blacklisted",),
        )

    def _incomplete_status(self, incomplete: set[str], *, manifest_hash: str = "") -> BaoStockRuntimeStatus:
        return BaoStockRuntimeStatus(
            state="completed_with_failures",
            sessions=self._options.sessions,
            universe_count=self._plan.universe_count,
            completed_codes=self._plan.universe_count - len(incomplete),
            failed_codes=len(incomplete),
            manifest_hash=manifest_hash,
            failure_reasons=tuple(sorted(self._failure_reasons or {"increment_required_field_missing"})),
        )


class _BaoStockIncrementSource:
    def __init__(self, sdk: object) -> None:
        self._sdk = cast(_IncrementSdk, sdk)

    def fetch_daily(
        self,
        code: str,
        family: BaoStockArchiveFieldFamily,
        dates: tuple[date, ...],
    ) -> BaoStockIncrementFetch:
        if family not in ("daily_raw", "daily_qfq", "is_st") or not dates:
            raise ValueError("BaoStock increment daily request is invalid")
        source_code = ("sh." if code.startswith("6") else "sz.") + code
        fields = _FACT_FIELDS if family == "is_st" else _DAILY_FIELDS
        adjustment = "3" if family in ("daily_raw", "is_st") else "2"
        expected = frozenset(dates)
        records: list[BaoStockIncrementRecord] = []
        observed: set[date] = set()
        unavailable: set[date] = set()
        windows = qfq_source_windows(source_code, dates) if family == "daily_qfq" else ((source_code, dates),)
        for query_code, query_dates in windows:
            rows = _rows(
                self._sdk.query_history_k_data_plus(
                    query_code,
                    fields,
                    query_dates[0].isoformat(),
                    query_dates[-1].isoformat(),
                    frequency="d",
                    adjustflag=adjustment,
                ),
                f"{family}_query_failed",
            )
            for row in rows:
                day = date.fromisoformat(_required(row, "date"))
                if day not in expected:
                    continue
                if _qfq_adjustment_unavailable(row, query_code, family, adjustment):
                    unavailable.add(day)
                    continue
                if day in observed:
                    raise ValueError("BaoStock increment response contains duplicate dates")
                observed.add(day)
                if family == "is_st":
                    records.append(_record(code, day, "is_st", {"is_st": _flag(row.get("isST"))}))
                    continue
                records.append(_daily_record(code, day, family, row))
                if family == "daily_raw":
                    records.append(_record(code, day, "is_st", {"is_st": _flag(row.get("isST"))}))
        if observed | unavailable != expected:
            raise RuntimeError("supplier_response_incomplete")
        unavailable_keys = tuple(BaoStockArchiveRecordKey(code, day, family) for day in unavailable)
        return BaoStockIncrementFetch(
            tuple(records),
            unavailable_keys,
            "supplier_adjustment_unavailable" if unavailable_keys else None,
        )

    def fetch_industry(
        self,
        codes: frozenset[str],
        *,
        as_of: date,
    ) -> tuple[BaoStockIncrementRecord, ...]:
        rows = _rows(
            self._sdk.query_stock_industry(code="", date=as_of.isoformat()),
            "industry_query_failed",
        )
        records: list[BaoStockIncrementRecord] = []
        identities: dict[tuple[str, date], BaoStockIncrementRecord] = {}
        for row in rows:
            source_code = row.get("code", "")
            code = source_code.split(".")[-1]
            effective = row.get("updateDate", "")
            industry = row.get("industry", "").strip()
            classification = row.get("industryClassification", "").strip()
            if code not in codes or not effective or not industry or not classification:
                continue
            effective_on = date.fromisoformat(effective)
            if effective_on > as_of:
                continue
            record = _record(
                code,
                effective_on,
                "industry",
                {
                    "effective_to": None,
                    "industry": industry,
                    "classification": classification,
                },
            )
            key = (code, effective_on)
            previous = identities.get(key)
            if previous is not None and previous != record:
                raise ValueError("BaoStock industry facts conflict at one effective date")
            identities[key] = record
        records.extend(identities.values())
        return tuple(sorted(records, key=lambda item: item.key))


def _qfq_adjustment_unavailable(
    row: dict[str, str],
    query_code: str,
    family: BaoStockArchiveFieldFamily,
    adjustment: str,
) -> bool:
    if row.get("code") != query_code or row.get("tradestatus") not in {"0", "1"}:
        raise ValueError("BaoStock increment row identity is invalid")
    if family == "is_st" or row.get("adjustflag") == adjustment:
        return False
    if family == "daily_qfq" and row.get("adjustflag") == "3":
        return True
    raise ValueError("BaoStock increment adjustment identity is invalid")


class _IncrementSdk(Protocol):
    __version__: str

    def query_trade_dates(self, *, start_date: str, end_date: str) -> BaoStockRowResult: ...

    def query_stock_industry(self, *, code: str = "", date: str = "") -> BaoStockRowResult: ...

    def query_history_k_data_plus(  # noqa: PLR0913 - exact BaoStock SDK protocol
        self,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        *,
        frequency: str,
        adjustflag: str,
    ) -> BaoStockRowResult: ...


def _increment_worker_main(
    connection: Connection,
    request: BaoStockRuntimeRequest,
    root_value: str,
    prepared: _PreparedIncrement | None,
) -> None:
    sdk = None
    try:
        root = Path(root_value)
        sdk = _load_sdk()
        _login(sdk)
        limited = _RateLimitedBaoStockSdk(
            sdk,
            activity=lambda state: connection.send(SupplierCallActivity(state)),
        )
        if prepared is None:
            target_dates = _fetch_target_calendar(root, limited)
            plan = build_archive_plan(root, target_open_dates=target_dates)
            context = _active_context(root, plan, limited.__version__)
            prepared = _PreparedIncrement(plan, context)
            connection.send(prepared)
        else:
            expected_context = _active_context(root, prepared.plan, limited.__version__)
            if expected_context != prepared.context:
                raise ValueError("BaoStock prepared increment context changed")
        archive = BaoStockActiveArchive(root, prepared.context)
        writer = archive.resume_writer()
        status = apply_increment_plan(
            prepared.plan,
            archive,
            writer,
            _BaoStockIncrementSource(limited),
            IncrementApplyOptions(
                sessions=request.sessions,
                retries=request.retries,
                report=connection.send,
            ),
        )
        connection.send(_IncrementWorkerResponse(status))
    except Exception as exc:
        connection.send(_IncrementWorkerResponse(None, _failure_code(exc)))
    finally:
        if sdk is not None:
            _logout(sdk)
        connection.close()


def _fetch_target_calendar(root: Path, sdk: _IncrementSdk) -> tuple[date, ...]:
    parent_cutoff, parent_calendar = _read_parent_calendar(root)
    now = datetime.now(_SHANGHAI)
    latest = now.date() if now.time() >= clock_time(15, 30) else now.date() - timedelta(days=1)
    if latest <= parent_cutoff:
        return parent_calendar
    rows = _rows(
        sdk.query_trade_dates(
            start_date=(parent_cutoff + timedelta(days=1)).isoformat(),
            end_date=latest.isoformat(),
        ),
        "calendar_query_failed",
    )
    extra = tuple(
        date.fromisoformat(_required(row, "calendar_date")) for row in rows if row.get("is_trading_day") == "1"
    )
    return tuple(sorted(set((*parent_calendar, *extra))))


def _read_parent_calendar(root: Path) -> tuple[date, tuple[date, ...]]:
    manifest = _json_object(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    partitions = cast(list[object], manifest.get("partitions"))
    first = _json_object(partitions[0])
    path = root / cast(str, first["relative_path"])
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True) as database:
        row = database.execute("SELECT spec_json, calendar_json FROM context WHERE singleton=1").fetchone()
    if row is None:
        raise ValueError("BaoStock parent context is unavailable")
    spec = _json_object(json.loads(cast(str, row[0])))
    calendar = _json_object(json.loads(cast(str, row[1])))
    return (
        date.fromisoformat(cast(str, spec["source_cutoff"])),
        tuple(date.fromisoformat(cast(str, item)) for item in cast(list[object], calendar["open_dates"])),
    )


def _active_context(root: Path, plan: ArchiveIncrementPlan, sdk_version: str) -> BaoStockActiveArchiveContext:
    manifest = _json_object(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    partition_by_code: list[tuple[str, str]] = []
    for raw in cast(list[object], manifest["partitions"]):
        item = _json_object(raw)
        partition = Path(cast(str, item["relative_path"])).stem
        partition_by_code.extend((cast(str, code), partition) for code in cast(list[object], item["codes"]))
    return BaoStockActiveArchiveContext(
        plan.parent_manifest_hash,
        plan.parent_manifest_file_hash,
        plan.target_source_cutoff,
        canonical_hash(plan.active_calendar_dates),
        canonical_hash(("baostock", sdk_version, _dependency_versions())),
        tuple(partition_by_code),
        plan.active_calendar_dates,
    )


def _planned_daily_requests(
    stock: StockArchivePlan,
    writer: BaoStockIncrementWriter,
) -> tuple[tuple[BaoStockArchiveFieldFamily, tuple[date, ...]], ...]:
    raw = tuple(
        day
        for day in stock.missing_raw_dates
        if not writer.completed(BaoStockArchiveRecordKey(stock.code, day, "daily_raw"))
    )
    qfq = tuple(
        day
        for day in stock.missing_qfq_dates
        if not writer.completed(BaoStockArchiveRecordKey(stock.code, day, "daily_qfq"))
    )
    is_st = tuple(
        day
        for day in stock.missing_is_st_dates
        if day not in raw and not writer.completed(BaoStockArchiveRecordKey(stock.code, day, "is_st"))
    )
    return (("daily_raw", raw), ("daily_qfq", qfq), ("is_st", is_st))


def _required_keys(stock: StockArchivePlan) -> set[BaoStockArchiveRecordKey]:
    values = {BaoStockArchiveRecordKey(stock.code, day, "daily_raw") for day in stock.missing_raw_dates}
    values.update(BaoStockArchiveRecordKey(stock.code, day, "daily_qfq") for day in stock.missing_qfq_dates)
    values.update(BaoStockArchiveRecordKey(stock.code, day, "is_st") for day in stock.missing_is_st_dates)
    return values


def _save_fetch_response(
    writer: BaoStockIncrementWriter,
    required: tuple[BaoStockArchiveRecordKey, ...],
    result: BaoStockIncrementFetch,
    *,
    family: BaoStockArchiveFieldFamily,
) -> None:
    required_set = set(required)
    if any(key not in required_set for key in result.unavailable_keys):
        raise ValueError("BaoStock increment unavailable key was not requested")
    for record in result.records:
        writer.save(record)
    if result.unavailable_reason is not None:
        for key in result.unavailable_keys:
            writer.record_failure(key, result.unavailable_reason)
    if family == "daily_raw":
        expected = {*required, *(BaoStockArchiveRecordKey(item.code, item.trade_date, "is_st") for item in required)}
    else:
        expected = set(required)
    unavailable = set(result.unavailable_keys)
    if any(not writer.completed(key) and key not in unavailable for key in expected):
        raise RuntimeError("supplier_response_incomplete")


def _field_coverage(
    plan: ArchiveIncrementPlan,
    writer: BaoStockIncrementWriter,
) -> tuple[BaoStockFieldCoverage, ...]:
    source = plan.field_coverage
    values = {
        "daily_raw": source.daily_raw,
        "daily_qfq": source.daily_qfq,
        "is_st": source.is_st,
        "industry": source.industry,
        "qualification": source.qualification,
        "hard_filter": source.hard_filter,
        "risk_facts": source.risk_facts,
    }
    result = []
    for family in BAOSTOCK_ARCHIVE_FIELD_FAMILIES:
        planned = values[family]
        incremental = writer.completed_count(family)
        missing = max(0, planned.missing_rows - incremental)
        reason = planned.missing_reason if missing else None
        if missing and reason is None:
            reason = "supplier_data_unavailable"
        result.append(BaoStockFieldCoverage(family, planned.reusable_rows, incremental, missing, reason))
    return tuple(result)


def _daily_record(
    code: str,
    day: date,
    family: BaoStockArchiveFieldFamily,
    row: dict[str, str],
) -> BaoStockIncrementRecord:
    suspended = row["tradestatus"] == "0"
    adjustment = "unadjusted" if family == "daily_raw" else "qfq"
    payload: dict[str, object] = {
        "adjustment": adjustment,
        "open_price": _number(row.get("open"), suspended=suspended),
        "high_price": _number(row.get("high"), suspended=suspended),
        "low_price": _number(row.get("low"), suspended=suspended),
        "close_price": _number(row.get("close"), suspended=suspended),
        "volume": _number(row.get("volume"), suspended=suspended, allow_zero=True),
        "amount": _number(row.get("amount"), suspended=suspended, allow_zero=True),
        "preclose": _number(row.get("preclose"), suspended=suspended) if family == "daily_raw" else None,
        "pct_change": _signed_ratio(row.get("pctChg"), suspended=suspended) if family == "daily_raw" else None,
        "turnover": _ratio(row.get("turn"), suspended=suspended) if family == "daily_raw" else None,
        "trading_status": "suspended" if suspended else "trading",
    }
    return _record(code, day, family, payload)


def _record(
    code: str,
    day: date,
    family: BaoStockArchiveFieldFamily,
    payload: dict[str, object],
) -> BaoStockIncrementRecord:
    document = {"code": code, "trade_date": day.isoformat(), **payload}
    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return BaoStockIncrementRecord(BaoStockArchiveRecordKey(code, day, family), encoded)


def _rows(result: BaoStockRowResult, failure: str) -> tuple[dict[str, str], ...]:
    if result.error_code != "0":
        raise RuntimeError("supplier_query_failed_blacklisted" if result.error_code == "10001011" else failure)
    fields = tuple(result.fields)
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("BaoStock result fields are invalid")
    rows = []
    while result.next():
        values = tuple(result.get_row_data())
        if len(values) != len(fields):
            raise ValueError("BaoStock result row width is invalid")
        rows.append(dict(zip(fields, values, strict=True)))
    if result.error_code != "0":
        raise RuntimeError("supplier_query_failed_blacklisted" if result.error_code == "10001011" else failure)
    return tuple(rows)


def _required(row: dict[str, str], key: str) -> str:
    value = row.get(key, "").strip()
    if not value:
        raise ValueError(f"BaoStock increment {key} is missing")
    return value


def _flag(value: str | None) -> bool:
    if value not in {"0", "1"}:
        raise ValueError("BaoStock increment boolean is invalid")
    return value == "1"


def _number(value: str | None, *, suspended: bool, allow_zero: bool = False) -> float | None:
    if value is None or not value.strip():
        if suspended:
            return None
        raise ValueError("BaoStock increment number is missing")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError("BaoStock increment number is invalid")
    return number


def _ratio(value: str | None, *, suspended: bool) -> float | None:
    number = _number(value, suspended=suspended, allow_zero=True)
    return None if number is None else number / 100.0


def _signed_ratio(value: str | None, *, suspended: bool) -> float | None:
    if value is None or not value.strip():
        if suspended:
            return None
        raise ValueError("BaoStock increment ratio is missing")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("BaoStock increment ratio is invalid")
    return number / 100.0


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("BaoStock JSON value is not an object")
    return cast(dict[str, object], value)


def _bounded_failure_code(exc: Exception) -> str:
    value = _failure_code(exc)
    return value if value and len(value) <= 64 else "supplier_query_failed"


__all__ = [
    "BaoStockIncrementSourcePort",
    "BaoStockIncrementFetch",
    "IncrementApplyOptions",
    "apply_increment_plan",
    "run_baostock_increment_update",
]
