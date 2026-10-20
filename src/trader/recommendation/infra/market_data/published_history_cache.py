"""Recommendation projection of the download-owned active history snapshot."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.published_history import PublishedHistoryManifest
from trader.infra.cache_contracts import CanonicalJsonBytes
from trader.infra.market_data.history.history import (
    BuildHistoryContext,
    DailyBar,
    HistoryContext,
    RequireQfqHistory,
)
from trader.recommendation.application.ports.market_data import MarketDataPendingError
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.epochs import DailyFeaturePack, DailyFeatureRow, DataPlaneCoverageEvidence
from trader.recommendation.domain.market.history_tail import HistoryQuality
from trader.recommendation.domain.market.quality import FieldQualityState, FieldValue
from trader.recommendation.infra.market_data.history_recovery import (
    HistoryRecovery,
    HistoryRecoveryStatus,
)
from trader.recommendation.infra.market_data.history_tail_recovery import (
    CandidateHistoryTailRecovery,
    HistoryTailStatus,
)
from trader.recommendation.infra.market_data.published_history_bars import OutcomeBars, QfqBars
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
    tail: HistoryTailStatus = HistoryTailStatus()
    disk_snapshot_hash: str | None = None
    outcome_snapshot_hash: str | None = None
    outcome_disk_snapshot_hash: str | None = None
    restart_required: bool = False


@dataclass(frozen=True, slots=True)
class PublishedHistoryEntry:
    bars: tuple[DailyBar, ...]
    context: HistoryContext


class PublishedHistoryCache:
    """Derived qfq feature/eligibility view with an independent full-history outcome reader."""

    def __init__(
        self,
        history: ReadPublishedHistoryUseCase,
        *,
        lookback_sessions: int,
        monotonic: Callable[[], float],
        recovery: HistoryRecovery | None = None,
        tail_recovery: CandidateHistoryTailRecovery | None = None,
        outcome_history: ReadPublishedHistoryUseCase | None = None,
        open_dates: Callable[[], tuple[date, ...]] | None = None,
        reserve_bytes: Callable[[int], bool] | None = None,
        require_input_binding: bool = False,
    ) -> None:
        self._history = history
        self._outcome_history = outcome_history
        self._monotonic = monotonic
        self._open_dates = open_dates
        self._reserve_bytes = reserve_bytes
        self._require_input_binding = require_input_binding
        self._lookback_sessions = max(61, lookback_sessions)
        self._recovery = recovery
        self._tail_recovery = tail_recovery
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._manifest: PublishedHistoryManifest | None = None
        self._outcome_manifest: PublishedHistoryManifest | None = None
        self._disk_snapshot_hash: str | None = None
        self._outcome_disk_snapshot_hash: str | None = None
        self._initialized = False
        self._daily_pack: DailyFeaturePack | None = None
        self._entry_bytes = 0
        self._entries: dict[str, PublishedHistoryEntry] = {}
        self._universe_rows = 0
        self._covered_rows = 0
        self._error_count = 0
        self._maintenance_state = "idle"
        self._maintenance_reason: str | None = None
        self._maintenance_stage: str | None = None
        self._maintenance_completed_units = 0
        self._maintenance_total_units = 0

    def initialize(self, *, cancelled: Callable[[], bool]) -> None:
        with self._refresh_lock:
            if self._initialized:
                return
            self._initialized = True
            deadline = self._monotonic() + 30.0
            try:
                self._outcome_manifest = self._outcome_history.manifest() if self._outcome_history else None
                self._outcome_disk_snapshot_hash = (
                    self._outcome_manifest.snapshot_hash if self._outcome_manifest else None
                )
            except (OSError, RuntimeError, ValueError):
                self._outcome_manifest = None
            for _ in range(3):
                if cancelled() or self._monotonic() >= deadline:
                    self._record_error("history_preload_cancelled" if cancelled() else "history_preload_deadline")
                    break
                if self._rebuild_projection(cancelled, deadline):
                    break
            self.record_projection_observation()

    def observe_publication(self) -> None:
        try:
            manifest = self._history.manifest()
            outcome = self._outcome_history.manifest() if self._outcome_history else None
        except (OSError, RuntimeError, ValueError) as exc:
            self._record_error(type(exc).__name__)
            self.record_projection_observation()
            return
        with self._lock:
            self._disk_snapshot_hash = manifest.snapshot_hash if manifest else None
            self._outcome_disk_snapshot_hash = outcome.snapshot_hash if outcome else None

    def _rebuild_projection(self, cancelled: Callable[[], bool], deadline: float) -> bool:
        try:
            manifest = self._history.manifest()
        except RuntimeError as exc:
            self._record_error(type(exc).__name__)
            return False
        if manifest is None:
            with self._lock:
                self._maintenance_reason = "history_snapshot_unavailable"
            return False
        try:
            entries = self._build_entries(manifest, cancelled, deadline)
            confirmed = self._history.manifest()
        except (RuntimeError, ValueError) as exc:
            self._record_error(type(exc).__name__)
            return False
        if not self._identity_stable(manifest, confirmed):
            return False
        size_bytes = self._reserve_projection(entries, cancelled, deadline)
        if size_bytes is None:
            return False
        with self._lock:
            self._manifest = manifest
            self._disk_snapshot_hash = manifest.snapshot_hash
            self._entries = entries
            self._entry_bytes = size_bytes
            self._universe_rows = len(manifest.universe_codes)
            self._covered_rows = sum(len(entry.bars) >= _RAW_RETENTION_SESSIONS for entry in entries.values())
            self._maintenance_reason = None
        return True

    def _reserve_projection(
        self, entries: Mapping[str, PublishedHistoryEntry], cancelled: Callable[[], bool], deadline: float
    ) -> int | None:
        if cancelled() or self._monotonic() >= deadline:
            self._record_error("history_preload_cancelled" if cancelled() else "history_preload_deadline")
            return None
        size_bytes = sum(len(CanonicalJsonBytes((code, entry))) for code, entry in entries.items())
        if cancelled() or self._monotonic() >= deadline:
            self._record_error("history_preload_cancelled" if cancelled() else "history_preload_deadline")
            return None
        if self._reserve_bytes is not None and not self._reserve_bytes(size_bytes):
            self._record_error("history_preload_capacity")
            return None
        return size_bytes

    @property
    def loaded_identity(self) -> str:
        with self._lock:
            return self._manifest.snapshot_hash if self._manifest else "qfq:unavailable"

    def bind_inputs(
        self,
        observed_at: datetime,
        *,
        versions: Mapping[str, str],
        config_version: str,
    ) -> None:
        """Attach provenance to the same startup contexts; no additional history reads."""
        observed_at = observed_at.astimezone(SHANGHAI)
        with self._lock:
            if self._daily_pack is not None or not self._entries or self._manifest is None:
                return
            rows = tuple(
                _daily_row(code, entry, observed_at, {**versions, "qfq": self._manifest.snapshot_hash})
                for code, entry in sorted(self._entries.items())
                if entry.context.latest_trade_date < observed_at.date().isoformat()
                or (entry.context.latest_trade_date == observed_at.date().isoformat() and observed_at.hour >= 15)
            )
            if not rows:
                return
            codes = tuple(row.code for row in rows)
            pack = DailyFeaturePack(
                observed_at.date(),
                self._manifest.sequence,
                observed_at,
                observed_at,
                config_version,
                versions.get("trading_calendar", "calendar:unavailable"),
                rows,
                {**versions, "qfq": self._manifest.snapshot_hash},
                DataPlaneCoverageEvidence((), codes if versions.get("security_master") else (), (), ()),
            )
            # Count the full entry index as well, including rows excluded by the
            # observation cutoff. The pack adds lineage without copying bars.
            size_bytes = self._entry_bytes + len(CanonicalJsonBytes(pack))
            if self._reserve_bytes is not None and not self._reserve_bytes(size_bytes):
                self._record_error("history_input_capacity")
                return
            self._daily_pack = pack

    @property
    def input_identity(self) -> str:
        with self._lock:
            return self._daily_pack.input_identity if self._daily_pack is not None else self.loaded_identity

    def _identity_stable(self, manifest: PublishedHistoryManifest, confirmed: PublishedHistoryManifest | None) -> bool:
        if confirmed is None or confirmed.snapshot_hash != manifest.snapshot_hash:
            self._record_error("history_snapshot_changed")
            return False
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

    def record_projection_observation(self) -> None:
        """Publish current read health atomically, preserving concurrent errors."""

        with self._lock:
            state = "ready" if self._manifest is not None else "unavailable"
            if self._maintenance_reason is not None and (
                self._manifest is not None or self._maintenance_reason != "history_snapshot_unavailable"
            ):
                state = "failed"
            self.record_maintenance(state, self._maintenance_reason, stage="reading_active_snapshot")

    def load(
        self,
        codes: Sequence[str],
        *,
        deadline: datetime | None = None,
        action_restrictions: dict[str, set[str]] | None = None,
        observed_at: datetime | None = None,
        recover_tail: bool = True,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        self.require_projection()
        result = self.cached(codes, observed_at=observed_at)
        if self._open_dates is not None and observed_at is not None:
            result = self._fresh_entries(result, observed_at)
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
        if self._tail_recovery is not None and observed_at is not None and recover_tail:
            result = self._load_tail(result, observed_at, deadline)
        if action_restrictions is not None:
            for code in dict.fromkeys(codes):
                if code not in result:
                    action_restrictions.setdefault(code, set()).add("history_data_pending")
        return result

    def require_projection(self) -> None:
        """Defer a realtime attempt while its first local projection is loading."""
        with self._lock:
            if self._require_input_binding and self._daily_pack is None:
                raise MarketDataPendingError(self._maintenance_reason or "history_inputs_loading")
            if self._manifest is None and (self._refresh_lock.locked() or self._maintenance_state == "loading"):
                raise MarketDataPendingError("history_projection_loading")

    def _load_tail(
        self,
        result: dict[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        deadline: datetime | None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        assert self._tail_recovery is not None
        with self._lock:
            manifest = self._manifest
        if manifest is None:
            # Standalone whole-window recovery has no weekly base to stitch.
            # Its existing independent-source qualification remains intact.
            return result
        stale = tuple(code for code, bars in result.items() if not self._fresh(bars, observed_at))
        if stale:
            recovered = self._tail_recovery.recover(
                stale,
                manifest,
                sessions=self._lookback_sessions,
                observed_at=observed_at,
                deadline=deadline,
            )
            with self._lock:
                if self._manifest == manifest:
                    result.update(recovered)
                else:
                    result = self.cached(tuple(result), observed_at=observed_at)
        return {code: bars for code, bars in result.items() if self._fresh(bars, observed_at)}

    def cached(
        self,
        codes: Iterable[str],
        *,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
        observed_at: datetime | None = None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(dict.fromkeys(codes))
        with self._lock:
            result = {code: entry.bars for code in requested if (entry := self._entries.get(code)) is not None}
            manifest = self._manifest
        if self._tail_recovery is not None and observed_at is not None:
            if manifest is not None:
                result.update(self._tail_recovery.cached(requested, manifest, self._lookback_sessions, observed_at))
            if fresh_only:
                result = {code: bars for code, bars in result.items() if self._fresh(bars, observed_at)}
        elif fresh_only and self._open_dates is not None and observed_at is not None:
            result = self._fresh_entries(result, observed_at)
        if action_restrictions is not None:
            for code in requested:
                if code not in result:
                    action_restrictions.setdefault(code, set()).add("history_data_pending")
        return result

    def _fresh_entries(
        self, entries: dict[str, tuple[DailyBar, ...]], observed_at: datetime
    ) -> dict[str, tuple[DailyBar, ...]]:
        assert self._open_dates is not None
        try:
            observed_at = observed_at.astimezone(SHANGHAI)
            dates = self._open_dates()
            completed = tuple(day for day in dates if day < observed_at.date())
            if not completed or not dates or dates[-1] < observed_at.date():
                return {}
        except (OSError, RuntimeError, ValueError):
            return {}
        previous, today = completed[-1].isoformat(), observed_at.date().isoformat()
        return {
            code: bars
            for code, bars in entries.items()
            if bars
            and (previous <= bars[-1].trade_date < today or (bars[-1].trade_date == today and observed_at.hour >= 15))
        }

    def _fresh(self, bars: tuple[DailyBar, ...], observed_at: datetime) -> bool:
        if self._tail_recovery is None:
            return True
        try:
            observed_at = observed_at.astimezone(SHANGHAI)
            if bars and bars[-1].trade_date == observed_at.date().isoformat() and observed_at.hour < 15:
                return False
            return (
                bool(bars)
                and self._tail_recovery.plan(date.fromisoformat(bars[-1].trade_date), observed_at).quality
                is HistoryQuality.FULL_HISTORY_READY
            )
        except (OSError, RuntimeError, ValueError):
            return False

    def summaries(
        self,
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
    ) -> Mapping[str, HistoryContext]:
        del observed_at
        RequireQfqHistory(histories)
        with self._lock:
            entries = dict(self._entries)
        return {
            code: entries[code].context
            if code in entries and entries[code].bars == bars
            else BuildHistoryContext(bars, lookback_sessions=self._lookback_sessions)
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
                tail=self._tail_recovery.status() if self._tail_recovery is not None else HistoryTailStatus(),
                disk_snapshot_hash=self._disk_snapshot_hash,
                outcome_snapshot_hash=self._outcome_manifest.snapshot_hash if self._outcome_manifest else None,
                outcome_disk_snapshot_hash=self._outcome_disk_snapshot_hash,
                restart_required=(
                    (manifest.snapshot_hash if manifest else None) != self._disk_snapshot_hash
                    or (self._outcome_manifest.snapshot_hash if self._outcome_manifest else None)
                    != self._outcome_disk_snapshot_hash
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
        try:
            manifest = self._outcome_manifest
            if manifest is None or self._outcome_history is None:
                return {}
            windows = self._outcome_history.read_windows(
                manifest,
                tuple(dict.fromkeys(codes)),
                sessions=self._lookback_sessions,
            )
        except (RuntimeError, ValueError) as exc:
            self._record_error(type(exc).__name__)
            return {}
        return {window.code: paired for window in windows if (paired := OutcomeBars(window.cells))}

    def _build_entries(
        self, manifest: PublishedHistoryManifest, cancelled: Callable[[], bool], deadline: float
    ) -> dict[str, PublishedHistoryEntry]:
        entries: dict[str, PublishedHistoryEntry] = {}
        for window in self._history.iter_windows(manifest, sessions=self._lookback_sessions):
            if cancelled() or self._monotonic() >= deadline:
                raise RuntimeError("history_preload_cancelled" if cancelled() else "history_preload_deadline")
            bars = QfqBars(window)
            if not bars:
                continue
            entries[window.code] = PublishedHistoryEntry(
                bars=bars[-_RAW_RETENTION_SESSIONS:],
                context=BuildHistoryContext(bars, lookback_sessions=self._lookback_sessions),
            )
        return entries

    def _record_error(self, reason: str) -> None:
        with self._lock:
            self._error_count += 1
            self._maintenance_reason = reason


def _daily_row(
    code: str,
    entry: PublishedHistoryEntry,
    observed_at: datetime,
    versions: Mapping[str, str],
) -> DailyFeatureRow:
    profile = entry.context.profile
    values = {
        "ma5": profile.moving_average_5d,
        "ma10": profile.moving_average_10d,
        "ma20": profile.moving_average_20d,
        "ma60": profile.moving_average_60d,
        "amihud_20d": profile.amihud_20d,
        "volatility_20d": profile.volatility_20d,
        "amount_median_20d": profile.median_amount_20d,
    }
    day = date.fromisoformat(entry.context.latest_trade_date)
    source_time = datetime.combine(day, time(15), tzinfo=SHANGHAI)
    fields = {
        name: FieldValue(
            name,
            value,
            "qfq",
            source_time,
            observed_at,
            versions["qfq"],
            versions["qfq"],
            FieldQualityState.MISSING if value is None else FieldQualityState.VALID,
        )
        for name, value in values.items()
    }
    return DailyFeatureRow(
        code,
        values,
        entry.context.sample_count,
        day,
        fields,
        security_master_version=versions.get("security_master", ""),
        history_version=versions["qfq"],
        missing_fields=tuple(name for name, value in values.items() if value is None),
    )


__all__ = ["PublishedHistoryCache", "PublishedHistoryEntry", "PublishedHistoryStatus"]
