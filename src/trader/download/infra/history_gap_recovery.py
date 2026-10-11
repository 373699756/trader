"""Run bounded BaoStock recovery before the slower full-universe partition writes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_price_qualification import qualify_history_pairs
from trader.download.domain.history_sync import (
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistoryTwoStageSupplier,
)
from trader.download.infra.history_tencent_stage import HistoryTencentStage

_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True)
class HistoryGapRecovery:
    code: str
    cells: tuple[BaoStockDailyCell, ...]


@dataclass(frozen=True)
class HistoryGapRecoveryContext:
    stage: HistoryTencentStage
    supplier: HistoryTwoStageSupplier
    universe: tuple[BaoStockSecurity, ...]
    selected_codes: tuple[str, ...]
    configuration: HistorySyncConfiguration
    progress: HistorySyncProgressPort | None
    cancel_requested: Callable[[], bool]
    monotonic: Callable[[], float]


@dataclass(frozen=True)
class _RecoveryOutcome:
    state: Literal["started", "completed", "failed"]
    failure_reason: str | None = None


def recover_selected_history_gaps(context: HistoryGapRecoveryContext) -> tuple[HistoryGapRecovery, ...]:
    started_at = context.monotonic()
    recovered: list[HistoryGapRecovery] = []
    for selected_index, code in enumerate(context.selected_codes):
        matched = next(
            (item for item in context.universe if item.code == code),
            None,
        )
        if matched is None:
            raise RuntimeError("history_recovery_selection_unknown")
        if context.cancel_requested():
            raise RuntimeError("cancelled")
        if context.monotonic() - started_at >= context.configuration.baostock_budget_seconds:
            break
        value = _recover_security(context, selected_index, matched)
        if value is not None:
            recovered.append(value)
    return tuple(recovered)


def _recover_security(
    context: HistoryGapRecoveryContext,
    index: int,
    security: BaoStockSecurity,
) -> HistoryGapRecovery | None:
    tencent, missing_days, anchor_days = context.stage.read_supplement_context(security.code)
    missing_dates = tuple(date.fromisoformat(day) for day in missing_days)
    if not missing_dates:
        return None
    price_dates = tuple(sorted(set(missing_dates) | {date.fromisoformat(day) for day in anchor_days}))
    requested_dates = tuple(sorted({cell.trade_date for cell in tencent.cells} | set(missing_dates)))
    _emit(context.progress, _progress(_RecoveryOutcome("started"), index, context, security.code, price_dates))
    cells: tuple[BaoStockDailyCell, ...] = ()
    failure_reason: str | None = None
    try:
        result = context.supplier.fetch_baostock_prices(security, price_dates)
        batch = result.batch
        if (
            batch.code != security.code
            or batch.failure_reasons
            or tuple(cell.trade_date for cell in batch.cells) != price_dates
            or any((batch.duplicate_rows, batch.null_rows, batch.out_of_window_rows, batch.future_rows))
        ):
            raise ValueError("history_recovery_invalid")
        if any(fact.is_st for fact in result.daily_facts):
            raise RuntimeError("history_st_reference_conflict")
        cells = batch.cells
        qualify_history_pairs(security.code, requested_dates, tencent, cells)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if context.cancel_requested():
            raise RuntimeError("cancelled") from exc
        cells = ()
        failure_reason = _failure_code(exc)
        if failure_reason == "history_st_reference_conflict":
            raise
    outcome = (
        _RecoveryOutcome("failed", failure_reason) if failure_reason is not None else _RecoveryOutcome("completed")
    )
    _emit(
        context.progress,
        _progress(outcome, index + 1, context, security.code, price_dates),
    )
    return HistoryGapRecovery(security.code, cells)


def _progress(
    outcome: _RecoveryOutcome,
    completed: int,
    context: HistoryGapRecoveryContext,
    code: str,
    dates: tuple[date, ...],
) -> HistorySyncProgress:
    return HistorySyncProgress(
        "baostock_gap_fill",
        outcome.state,
        completed,
        len(context.selected_codes),
        code,
        supplier_source="baostock",
        requested_sessions=len(dates),
        failure_reason=outcome.failure_reason,
    )


def _emit(progress: HistorySyncProgressPort | None, value: HistorySyncProgress) -> None:
    if progress is None:
        return
    try:
        progress.publish(value)
    except OSError:
        pass


def _failure_code(exc: BaseException) -> str:
    value = str(exc).strip()
    return value if _ERROR_CODE.fullmatch(value) else "history_recovery_failed"
