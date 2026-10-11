"""Recover every history gap with durable, bounded per-security attempts."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockSecurity
from trader.download.domain.history_price_qualification import qualify_history_pairs
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import (
    HistoryRecoverySummary,
    HistorySyncProgress,
    HistorySyncProgressPort,
    HistoryTwoStageSupplier,
)
from trader.download.infra.history_tencent_stage import HistoryTencentStage

_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
_FATAL_FAILURES = frozenset({"history_st_reference_conflict", "history_supplier_identity_conflict"})
_TERMINAL_FAILURES = frozenset(
    {
        "history_tail_qfq_basis_conflict",
        "history_tail_raw_price_conflict",
        "history_recovery_anchor_missing",
        "supplier_daily_window_conflict",
        "supplier_daily_facts_mismatch",
    }
)


@dataclass(frozen=True)
class HistoryGapRecoveryContext:
    stage: HistoryTencentStage
    supplier: HistoryTwoStageSupplier
    universe: tuple[BaoStockSecurity, ...]
    selected_codes: tuple[str, ...]
    sequence: int
    progress: HistorySyncProgressPort | None
    cancel_requested: Callable[[], bool]


@dataclass(frozen=True)
class _RecoveryOutcome:
    state: Literal["started", "completed", "failed", "retrying"]
    attempt: int
    failure_reason: str | None = None


def recover_selected_history_gaps(context: HistoryGapRecoveryContext) -> None:
    succeeded = failed = 0
    try:
        for index, code in enumerate(context.selected_codes):
            _check_cancel(context)
            security = next((item for item in context.universe if item.code == code), None)
            if security is None:
                raise RuntimeError("history_recovery_selection_unknown")
            if _recover_security(context, index, security):
                succeeded += 1
            else:
                failed += 1
    finally:
        remaining = len(context.selected_codes) - succeeded - failed
        _emit(
            context.progress,
            HistorySyncProgress(
                "baostock_gap_fill",
                "completed" if remaining == 0 else "cancelled" if context.cancel_requested() else "failed",
                succeeded + failed,
                len(context.selected_codes),
                recovery_summary=HistoryRecoverySummary(succeeded, failed, remaining),
            ),
        )


def _recover_security(context: HistoryGapRecoveryContext, index: int, security: BaoStockSecurity) -> bool:
    checkpoint = context.stage.recovery_checkpoint(security.code)
    if checkpoint.failure_reason in _FATAL_FAILURES:
        raise RuntimeError(checkpoint.failure_reason)
    if checkpoint.succeeded or checkpoint.attempts == 3 or checkpoint.failure_reason in _TERMINAL_FAILURES:
        return checkpoint.succeeded
    if context.stage.is_supplemented(security.code):
        return True
    tencent, missing_days, anchor_days = context.stage.read_supplement_context(security.code)
    if not missing_days:
        raise RuntimeError("history_recovery_gap_inventory_conflict")
    dates = tuple(date.fromisoformat(day) for day in sorted(set(missing_days) | set(anchor_days)))
    requested = tuple(sorted({cell.trade_date for cell in tencent.cells} | set(dates)))
    for _ in range(checkpoint.attempts, 3):
        _check_cancel(context)
        attempt = context.stage.begin_recovery_attempt(security.code)
        _emit(
            context.progress,
            _progress(
                _RecoveryOutcome("started" if attempt == 1 else "retrying", attempt),
                index,
                context,
                security.code,
                dates,
            ),
        )
        cells: tuple[BaoStockDailyCell, ...] = ()
        failure: str | None = None
        try:
            cells = _fetch_pairs(context, security, dates)
            qualify_history_pairs(security.code, requested, tencent, cells)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if context.cancel_requested():
                raise RuntimeError("cancelled") from exc
            cells = ()
            failure = _failure_code(exc)
        _check_cancel(context)
        context.stage.finish_recovery_attempt(
            security.code,
            failure,
            tuple(HistoryRevision(context.sequence, security.board, cell, None, None, None) for cell in cells),
        )
        terminal = failure is None or attempt == 3 or failure in _TERMINAL_FAILURES
        _emit(
            context.progress,
            _progress(
                _RecoveryOutcome("completed" if failure is None else "failed", attempt, failure),
                index + int(terminal),
                context,
                security.code,
                dates,
            ),
        )
        if failure in _FATAL_FAILURES:
            raise RuntimeError(failure)
        if terminal:
            return failure is None
    return False


def _fetch_pairs(
    context: HistoryGapRecoveryContext, security: BaoStockSecurity, dates: tuple[date, ...]
) -> tuple[BaoStockDailyCell, ...]:
    result = context.supplier.fetch_baostock_prices(security, dates)
    batch = result.batch
    if batch.code != security.code:
        raise RuntimeError("history_supplier_identity_conflict")
    if any(fact.code != security.code for fact in result.daily_facts):
        raise RuntimeError("history_supplier_identity_conflict")
    if any(fact.is_st for fact in result.daily_facts):
        raise RuntimeError("history_st_reference_conflict")
    if (
        batch.failure_reasons
        or tuple(cell.trade_date for cell in batch.cells) != dates
        or any((batch.duplicate_rows, batch.null_rows, batch.out_of_window_rows, batch.future_rows))
        or any(not cell.obtained for cell in batch.cells)
    ):
        raise ValueError("history_recovery_invalid")
    return batch.cells


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
        attempt=outcome.attempt,
        max_attempts=3,
        supplier_source="baostock",
        requested_sessions=len(dates),
        failure_reason=outcome.failure_reason,
    )


def _check_cancel(context: HistoryGapRecoveryContext) -> None:
    if context.cancel_requested():
        raise RuntimeError("cancelled")


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
