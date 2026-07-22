"""Non-blocking daily-history warmup for the live recommendation path."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime

from trader.application.source_lanes import SourceRequestSuperseded
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_history import HistoryStore
from trader.infra.market_data.service_support import _normalize_codes, _source_batch_identity
from trader.infra.market_data.service_tushare import ReferenceLoader

_LOGGER = logging.getLogger(__name__)
_HISTORY_SOURCE_LANE = "history"
_PERMANENT_TUSHARE_DEGRADATIONS = frozenset({"missing_token", "insufficient_points", "permission_denied"})


@dataclass(frozen=True)
class HistoryWarmupStatus:
    planned_count: int
    completed_count: int
    failure_count: int
    inflight_count: int
    last_source: str


class HistoryWarmup:
    def __init__(
        self,
        history: HistoryStore,
        references: ReferenceLoader,
        runner: MarketTaskRunner,
        *,
        batch_size: int,
        monotonic: Callable[[], float],
    ) -> None:
        self._history = history
        self._references = references
        self._runner = runner
        self._batch_size = max(1, batch_size)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._universe: tuple[str, ...] = ()
        self._inflight: set[str] = set()
        self._planned_count = 0
        self._completed_count = 0
        self._failure_count = 0
        self._last_source = ""

    def schedule_history_warmup(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> None:
        normalized = _normalize_codes(codes)
        lanes = self._runner.source_lanes
        if not normalized or lanes is None:
            return
        now = self._monotonic()
        with self._lock:
            self._universe = normalized
            if self._inflight:
                return
            entries = self._history.entries()
            missing = tuple(
                code
                for code in normalized
                if code not in self._inflight and ((entry := entries.get(code)) is None or entry.expires_at <= now)
            )
        if not missing:
            return

        try:
            local_seed_codes = self._history.available_seed_codes(missing)
        except Exception as exc:
            local_seed_codes = ()
            _LOGGER.warning("local history seed discovery degraded: %s", type(exc).__name__)
        tushare_health = dict(self._references.health())
        use_tushare = (
            not local_seed_codes
            and bool(tushare_health.get("enabled"))
            and not bool(tushare_health.get("circuit_open"))
            and tushare_health.get("degraded_reason") not in _PERMANENT_TUSHARE_DEGRADATIONS
            and tushare_health.get("history_mode") == "forward_adjusted"
        )
        source = "local_seed" if local_seed_codes else ("tushare" if use_tushare else "tencent")
        batch = (local_seed_codes or missing)[: self._batch_size]
        with self._lock:
            self._inflight.update(batch)
            self._planned_count += len(batch)
            self._last_source = source

        identity = _source_batch_identity("history_warmup", batch, observed_at, source=source)
        future: Future[object]
        if use_tushare:
            future = lanes.submit(
                "tushare",
                identity,
                observed_at,
                self._warm_tushare_history_batch,
                batch,
                observed_at,
            )
        else:
            future = lanes.submit(
                _HISTORY_SOURCE_LANE,
                identity,
                observed_at,
                self._history.load,
                batch,
            )
        future.add_done_callback(lambda completed: self._finish_history_warmup(batch, completed))

    def _warm_tushare_history_batch(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> None:
        observations = self._references.load_history_batch(codes, observed_at, force=False)
        self._references.apply_history(observations)

    def _finish_history_warmup(
        self,
        codes: Sequence[str],
        future: Future[object],
    ) -> None:
        superseded = False
        try:
            future.result()
        except SourceRequestSuperseded:
            superseded = True
        except Exception as exc:
            _LOGGER.warning("history warmup batch degraded: %s", type(exc).__name__)
        now = self._monotonic()
        entries = self._history.entries()
        with self._lock:
            self._inflight.difference_update(codes)
            covered = sum(
                (entry := entries.get(code)) is not None and entry.expires_at > now and len(entry.bars) >= 20
                for code in codes
            )
            self._completed_count += covered
            if not superseded:
                self._failure_count += max(0, len(codes) - covered)
            universe = self._universe
        self._history.update_coverage(universe)
        lanes = self._runner.source_lanes
        if universe and lanes is not None and not lanes.is_stopped("history") and not lanes.is_stopped("tushare"):
            self.schedule_history_warmup(universe, self._runner.wall_clock())

    def status(self) -> HistoryWarmupStatus:
        with self._lock:
            return HistoryWarmupStatus(
                planned_count=self._planned_count,
                completed_count=self._completed_count,
                failure_count=self._failure_count,
                inflight_count=len(self._inflight),
                last_source=self._last_source,
            )


__all__ = ["HistoryWarmup", "HistoryWarmupStatus"]
