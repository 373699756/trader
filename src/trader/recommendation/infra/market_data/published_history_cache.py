"""Recommendation projection of the download-owned active history snapshot."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.infra.market_data.history.history import (
    DailyBar,
    HistoryContext,
    PriceAdjustment,
    build_history_context,
    require_qfq_history,
)
from trader.infra.market_data.history.outcome_history import pair_outcome_history
from trader.recommendation.infra.market_data.history_recovery import (
    HistoryRecovery,
    HistoryRecoveryStatus,
)
from trader.training.domain.evaluation.models import OutcomeBar

_RAW_RETENTION_SESSIONS = 20


@dataclass(frozen=True, slots=True)
class PublishedHistoryStatus:
    state: str
    snapshot_hash: str | None
    data_cutoff: date | None
    entries: int
    raw_rows: int
    profile_entries: int
    universe_rows: int
    covered_rows: int
    error_count: int
    data_versions: tuple[str, ...]
    out_of_order_count: int
    maintenance_state: str
    maintenance_reason: str | None
    maintenance_stage: str | None
    maintenance_completed_units: int
    maintenance_total_units: int
    recovery: HistoryRecoveryStatus = HistoryRecoveryStatus(0, 0, 0, 0, None, None)


@dataclass(frozen=True, slots=True)
class PublishedHistoryEntry:
    bars: tuple[DailyBar, ...]
    context: HistoryContext


class PublishedHistoryCache:
    """Derived, rebuildable feature view; never downloads or persists history."""

    def __init__(
        self,
        history: ReadPublishedHistoryUseCase,
        *,
        lookback_sessions: int,
        recovery: HistoryRecovery | None = None,
    ) -> None:
        self._history = history
        self._lookback_sessions = max(61, lookback_sessions)
        self._recovery = recovery
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._manifest: PublishedHistoryManifest | None = None
        self._entries: dict[str, PublishedHistoryEntry] = {}
        self._universe_rows = 0
        self._covered_rows = 0
        self._error_count = 0
        self._maintenance_state = "idle"
        self._maintenance_reason: str | None = None
        self._maintenance_stage: str | None = None
        self._maintenance_completed_units = 0
        self._maintenance_total_units = 0

    def refresh(self) -> bool:
        with self._refresh_lock:
            try:
                manifest = self._history.manifest()
            except RuntimeError as exc:
                self._record_error(type(exc).__name__)
                return False
            if manifest is None:
                with self._lock:
                    if self._manifest is None:
                        self._maintenance_reason = "history_snapshot_unavailable"
                return False
            with self._lock:
                if self._manifest is not None and self._manifest.snapshot_hash == manifest.snapshot_hash:
                    return False
            try:
                entries = self._build_entries(manifest)
                confirmed = self._history.manifest()
            except (RuntimeError, ValueError) as exc:
                self._record_error(type(exc).__name__)
                return False
            if confirmed is None or confirmed.snapshot_hash != manifest.snapshot_hash:
                self._record_error("history_snapshot_changed")
                return False
            with self._lock:
                self._manifest = manifest
                self._entries = entries
                self._universe_rows = len(manifest.universe_codes)
                self._covered_rows = sum(len(entry.bars) >= _RAW_RETENTION_SESSIONS for entry in entries.values())
                self._maintenance_reason = None
            return True

    def record_maintenance(
        self,
        state: str,
        reason: str | None = None,
        *,
        stage: str | None = None,
        completed_units: int = 0,
        total_units: int = 0,
    ) -> None:
        with self._lock:
            self._maintenance_state = state
            self._maintenance_reason = reason
            self._maintenance_stage = stage
            self._maintenance_completed_units = completed_units
            self._maintenance_total_units = total_units

    def load(
        self,
        codes: Sequence[str],
        *,
        force: bool = False,
        deadline: datetime | None = None,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        del force
        self.refresh()
        result = self.cached(codes)
        if self._recovery is not None:
            missing = tuple(code for code in dict.fromkeys(codes) if code not in result)
            if missing:
                result.update(
                    self._recovery.recover(
                        missing,
                        days=self._lookback_sessions,
                        deadline=deadline,
                    )
                )
        if action_restrictions is not None:
            for code in dict.fromkeys(codes):
                if code not in result:
                    action_restrictions.setdefault(code, set()).add("history_data_pending")
        return result

    def cached(
        self,
        codes: Iterable[str],
        *,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        del fresh_only
        requested = tuple(dict.fromkeys(codes))
        with self._lock:
            result = {code: entry.bars for code in requested if (entry := self._entries.get(code)) is not None}
        if action_restrictions is not None:
            for code in requested:
                if code not in result:
                    action_restrictions.setdefault(code, set()).add("history_data_pending")
        return result

    def summaries(
        self,
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
    ) -> Mapping[str, HistoryContext]:
        del observed_at
        require_qfq_history(histories)
        with self._lock:
            entries = dict(self._entries)
        return {
            code: entries[code].context if code in entries else build_history_context(bars, lookback_sessions=self._lookback_sessions)
            for code, bars in histories.items()
            if bars
        }

    def update_coverage(self, codes: Sequence[str], data_versions: Sequence[str] | None = None) -> None:
        del data_versions
        requested = tuple(dict.fromkeys(codes))
        with self._lock:
            self._universe_rows = len(requested)
            self._covered_rows = sum(
                (entry := self._entries.get(code)) is not None and len(entry.bars) >= _RAW_RETENTION_SESSIONS
                for code in requested
            )

    def status(self) -> PublishedHistoryStatus:
        with self._lock:
            manifest = self._manifest
            return PublishedHistoryStatus(
                state="active" if manifest is not None else "unavailable",
                snapshot_hash=manifest.snapshot_hash if manifest is not None else None,
                data_cutoff=manifest.data_cutoff if manifest is not None else None,
                entries=len(self._entries),
                raw_rows=sum(len(entry.bars) for entry in self._entries.values()),
                profile_entries=len(self._entries),
                universe_rows=self._universe_rows,
                covered_rows=self._covered_rows,
                error_count=self._error_count,
                data_versions=(manifest.snapshot_hash,) if manifest is not None else (),
                out_of_order_count=0,
                maintenance_state=self._maintenance_state,
                maintenance_reason=self._maintenance_reason,
                maintenance_stage=self._maintenance_stage,
                maintenance_completed_units=self._maintenance_completed_units,
                maintenance_total_units=self._maintenance_total_units,
                recovery=(
                    self._recovery.status()
                    if self._recovery is not None
                    else HistoryRecoveryStatus(0, 0, 0, 0, None, None)
                ),
            )

    def entries(self) -> Mapping[str, PublishedHistoryEntry]:
        with self._lock:
            return dict(self._entries)

    def read_outcome_bars(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[OutcomeBar, ...]]:
        del observed_at
        self.refresh()
        with self._lock:
            manifest = self._manifest
        if manifest is None:
            return {}
        try:
            windows = self._history.read_windows(
                manifest,
                tuple(dict.fromkeys(codes)),
                sessions=self._lookback_sessions,
            )
        except (RuntimeError, ValueError) as exc:
            self._record_error(type(exc).__name__)
            return {}
        return {
            window.code: paired
            for window in windows
            if (paired := _outcome_bars(window.revisions))
        }

    def _build_entries(self, manifest: PublishedHistoryManifest) -> dict[str, PublishedHistoryEntry]:
        entries: dict[str, PublishedHistoryEntry] = {}
        for window in self._history.iter_windows(manifest, sessions=self._lookback_sessions):
            bars = _qfq_bars(window)
            if not bars:
                continue
            entries[window.code] = PublishedHistoryEntry(
                bars=bars[-_RAW_RETENTION_SESSIONS:],
                context=build_history_context(bars, lookback_sessions=self._lookback_sessions),
            )
        return entries

    def _record_error(self, reason: str) -> None:
        with self._lock:
            self._error_count += 1
            self._maintenance_reason = reason


def _qfq_bars(window: PublishedHistoryWindow) -> tuple[DailyBar, ...]:
    bars = tuple(
        bar
        for revision in window.revisions
        if (bar := _daily_bar(revision, PriceAdjustment.QFQ)) is not None
    )
    return tuple(sorted(bars, key=lambda item: item.trade_date))


def _outcome_bars(revisions: tuple[HistoryRevision, ...]) -> tuple[OutcomeBar, ...]:
    qfq = tuple(
        bar
        for revision in revisions
        if (bar := _daily_bar(revision, PriceAdjustment.QFQ)) is not None
    )
    raw = tuple(
        bar
        for revision in revisions
        if (bar := _daily_bar(revision, PriceAdjustment.RAW)) is not None
    )
    return pair_outcome_history(qfq, raw)


def _daily_bar(revision: HistoryRevision, adjustment: PriceAdjustment) -> DailyBar | None:
    side = revision.cell.qfq if adjustment is PriceAdjustment.QFQ else revision.cell.unadjusted
    raw = revision.cell.unadjusted
    if side is None or raw is None:
        return None
    values = (side.open_price, side.close_price, side.high_price, side.low_price, side.volume, side.amount)
    if any(value is None for value in values):
        return None
    open_price, close_price, high_price, low_price, volume, amount = cast(tuple[float, ...], values)
    return DailyBar(
        trade_date=side.trade_date.isoformat(),
        open_price=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        volume=volume,
        amount=amount,
        pct_change=float(raw.pct_change or 0.0),
        turnover_rate=raw.turnover,
        adjustment=adjustment,
        source="baostock",
    )


__all__ = ["PublishedHistoryCache", "PublishedHistoryEntry", "PublishedHistoryStatus"]
