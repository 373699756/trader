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
from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality
from trader.recommendation.domain.market.history_tail import HistoryQuality
from trader.recommendation.domain.market.quality import FieldQualityState, FieldValue
from trader.recommendation.domain.market.v1.features import (
    LEARNED_RANK_FEATURE_IDS,
    CalculateLearnedRankFeatures,
    LearnedRankDailyPoint,
)
from trader.recommendation.domain.market.v5.features import (
    CalculatePriceVolumeSequence,
    ModelPriceVolumeSequence,
    PriceVolumePoint,
)
from trader.recommendation.infra.market_data.aligned_history_bars import BuildAlignedHistoryBars
from trader.recommendation.infra.market_data.history_recovery import (
    HistoryRecovery,
    HistoryRecoveryStatus,
)
from trader.recommendation.infra.market_data.history_tail_recovery import (
    CandidateHistoryTailRecovery,
    HistoryTailStatus,
)
from trader.recommendation.infra.market_data.published_history_bars import OutcomeBars
from trader.training.domain.evaluation.models import OutcomeBar

_RAW_RETENTION_SESSIONS = 20
_STARTUP_PRELOAD_DEADLINE_SECONDS = 300.0


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
    estimated_history_codes: int = 0
    suspended_history_codes: int = 0


@dataclass(frozen=True, slots=True)
class CompletedRankInput:
    through: date
    values: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PublishedHistoryEntry:
    bars: tuple[DailyBar, ...]
    context: HistoryContext
    learned_rank_inputs: tuple[CompletedRankInput, ...] = ()
    model_sequences: tuple[ModelPriceVolumeSequence, ...] = ()
    estimated_sessions: int = 0
    suspended_sessions: int = 0
    latest_observed_date: date | None = None


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
        learned_rank_inputs: bool = False,
        sequence_inputs: bool = False,
    ) -> None:
        self._history = history
        self._outcome_history = outcome_history
        self._monotonic = monotonic
        self._open_dates = open_dates
        self._reserve_bytes = reserve_bytes
        self._require_input_binding = require_input_binding
        self._learned_rank_inputs = learned_rank_inputs
        self._sequence_inputs = sequence_inputs
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
            deadline = self._monotonic() + _STARTUP_PRELOAD_DEADLINE_SECONDS
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
            self.record_maintenance(
                state,
                self._maintenance_reason,
                stage="reading_active_snapshot",
                completed_units=self._maintenance_completed_units,
                total_units=self._maintenance_total_units,
            )

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
        with self._lock:
            observed_dates = {
                code: entry.latest_observed_date.isoformat() if entry.latest_observed_date is not None else ""
                for code, entry in self._entries.items()
            }
        return {
            code: bars
            for code, bars in entries.items()
            if bars
            and (
                previous <= observed_dates.get(code, bars[-1].trade_date) < today
                or (observed_dates.get(code, bars[-1].trade_date) == today and observed_at.hour >= 15)
            )
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

    def completed_model_features(
        self, codes: Sequence[str], observed_at: datetime
    ) -> Mapping[str, tuple[tuple[str, float], ...]]:
        """Project startup-only complete-session inputs without reading the database."""
        if not self._learned_rank_inputs:
            return {}
        day = observed_at.astimezone(SHANGHAI).date()
        previous = (
            max((value for value in self._open_dates() if value < day), default=None) if self._open_dates else None
        )
        if previous is None:
            return {}
        with self._lock:
            return {
                code: tuple(zip(LEARNED_RANK_FEATURE_IDS, candidate.values, strict=True))
                for code in codes
                if (entry := self._entries.get(code)) is not None
                for candidate in entry.learned_rank_inputs
                if candidate.through == previous
            }

    def model_history_quality(self, code: str, observed_at: datetime) -> ModelHistoryQuality:
        with self._lock:
            manifest, entry = self._manifest, self._entries.get(code)
        if manifest is None:
            return ModelHistoryQuality("history_snapshot_unavailable")
        if entry is None:
            return ModelHistoryQuality("history_invalid_prices")
        failure = None
        day = observed_at.astimezone(SHANGHAI).date()
        previous = (
            max((value for value in self._open_dates() if value < day), default=None) if self._open_dates else None
        )
        if entry.context.sample_count < self._lookback_sessions:
            failure = "history_window_insufficient"
        elif entry.context.latest_trade_date > day.isoformat():
            failure = "history_model_date_mismatch"
        elif previous is not None and entry.context.latest_trade_date < previous.isoformat():
            failure = "history_model_date_mismatch"
        elif self._learned_rank_inputs and not any(item.through == previous for item in entry.learned_rank_inputs):
            failure = "history_window_insufficient" if entry.learned_rank_inputs else "history_invalid_prices"
        elif self._sequence_inputs and not any(item.dates[-1] == previous for item in entry.model_sequences):
            failure = "history_window_insufficient"
        return ModelHistoryQuality(failure, entry.estimated_sessions, entry.suspended_sessions)

    @property
    def completed_features_enabled(self) -> bool:
        return self._learned_rank_inputs or self._sequence_inputs

    def completed_model_sequences(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, ModelPriceVolumeSequence]:
        if not self._sequence_inputs or self._open_dates is None:
            return {}
        day = observed_at.astimezone(SHANGHAI).date()
        previous = max((d for d in self._open_dates() if d < day), default=None)
        with self._lock:
            return {
                code: seq
                for code in codes
                if (entry := self._entries.get(code)) is not None
                for seq in entry.model_sequences
                if seq.dates[-1] == previous
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
                estimated_history_codes=sum(entry.estimated_sessions > 0 for entry in self._entries.values()),
                suspended_history_codes=sum(entry.suspended_sessions > 0 for entry in self._entries.values()),
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
        lifetimes = {code: (start, end) for code, start, end in manifest.listing_intervals}
        read_sessions = min(self._lookback_sessions + 1, len(manifest.calendar_dates))
        completed = 0
        for completed, window in enumerate(self._history.iter_windows(manifest, sessions=read_sessions), start=1):
            self.record_maintenance(
                "loading",
                stage="reading_active_snapshot",
                completed_units=completed - 1,
                total_units=len(manifest.universe_codes),
            )
            if cancelled() or self._monotonic() >= deadline:
                raise RuntimeError("history_preload_cancelled" if cancelled() else "history_preload_deadline")
            aligned = BuildAlignedHistoryBars(
                window, manifest.calendar_dates[-read_sessions:], lifetimes.get(window.code)
            )
            if len(aligned.bars) < read_sessions:
                lifetime = lifetimes.get(window.code)
                # Only a leading no-trade interval needs an older valuation seed.
                # Keep the normal full-universe scan bounded by the feature window.
                first = manifest.calendar_dates[-read_sessions]
                if (lifetime is None or lifetime[0] <= first) and all(
                    cell.status in {"unknown_missing", "tencent_inferred_suspended", "supplier_marked_suspended"}
                    for cell in window.cells
                    if not aligned.bars or cell.trade_date < date.fromisoformat(aligned.bars[0].trade_date)
                ):
                    maximum = min(2000, len(manifest.calendar_dates))
                    extended_sessions = read_sessions
                    while len(aligned.bars) < read_sessions and extended_sessions < maximum:
                        if cancelled() or self._monotonic() >= deadline:
                            raise RuntimeError(
                                "history_preload_cancelled" if cancelled() else "history_preload_deadline"
                            )
                        extended_sessions = min(maximum, extended_sessions + 63)
                        extended = self._history.read_windows(manifest, (window.code,), sessions=extended_sessions)
                        if not extended:
                            break
                        aligned = BuildAlignedHistoryBars(
                            extended[0], manifest.calendar_dates[-extended_sessions:], lifetime
                        )
            bars = aligned.bars
            if not bars:
                continue
            bars = bars[-(self._lookback_sessions + 1) :]
            entries[window.code] = PublishedHistoryEntry(
                bars=bars[-_RAW_RETENTION_SESSIONS:],
                context=BuildHistoryContext(bars, lookback_sessions=self._lookback_sessions),
                learned_rank_inputs=tuple(
                    CompletedRankInput(date.fromisoformat(selected[-1].trade_date), values)
                    for selected in (bars, bars[:-1])
                    if self._learned_rank_inputs
                    and selected
                    and (values := _completed_model_values(selected, self._open_dates() if self._open_dates else ()))
                    is not None
                ),
                model_sequences=_model_sequences(
                    window.code, bars, self._open_dates() if self._open_dates else (), manifest.snapshot_hash
                )
                if self._sequence_inputs
                else (),
                estimated_sessions=sum(
                    day >= date.fromisoformat(bars[0].trade_date) for day in aligned.estimated_dates
                ),
                suspended_sessions=sum(
                    day >= date.fromisoformat(bars[0].trade_date) for day in aligned.suspended_dates
                ),
                latest_observed_date=aligned.latest_observed_date,
            )
        self.record_maintenance(
            "loading",
            stage="reading_active_snapshot",
            completed_units=completed,
            total_units=len(manifest.universe_codes),
        )
        return entries

    def _record_error(self, reason: str) -> None:
        with self._lock:
            self._error_count += 1
            self._maintenance_reason = reason


def _model_sequences(
    code: str, bars: tuple[DailyBar, ...], calendar: tuple[date, ...], source_identity: str
) -> tuple[ModelPriceVolumeSequence, ...]:
    sequences: list[ModelPriceVolumeSequence] = []
    for selected in (bars[-61:], bars[-62:-1]):
        if len(selected) != 61:
            continue
        end = date.fromisoformat(selected[-1].trade_date)
        dates = tuple(d for d in calendar if d <= end)[-61:]
        try:
            points = tuple(
                PriceVolumePoint(
                    date.fromisoformat(b.trade_date), b.open_price, b.high, b.low, b.close, b.volume, b.amount
                )
                for b in selected
            )
            sequences.append(
                CalculatePriceVolumeSequence(code, points, expected_dates=dates, source_identity=source_identity)
            )
        except ValueError:
            continue
    return tuple(sequences)


def _completed_model_values(bars: tuple[DailyBar, ...], open_dates: tuple[date, ...]) -> tuple[float, ...] | None:
    if len(bars) < 251:
        return None
    selected = bars[-251:]
    if any(bar.close <= 0.0 or bar.amount < 0.0 for bar in selected):
        return None
    dates = tuple(date.fromisoformat(bar.trade_date) for bar in selected)
    expected = tuple(day for day in open_dates if day <= dates[-1])[-251:]
    if dates != expected:
        return None
    return CalculateLearnedRankFeatures(
        tuple(LearnedRankDailyPoint(day, bar.close, bar.amount) for day, bar in zip(dates, selected, strict=True))
    )


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
