"""Non-blocking daily-history warmup for the live recommendation path."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import Future
from datetime import datetime, timedelta

from trader.application.schedule import shanghai_now
from trader.application.source_lanes import SourceRequestSuperseded
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.service_state import MarketServiceState
from trader.infra.market_data.service_support import _normalize_codes, _source_batch_identity

_LOGGER = logging.getLogger(__name__)
_HISTORY_SOURCE_LANE = "history"
_PERMANENT_TUSHARE_DEGRADATIONS = frozenset({"missing_token", "insufficient_points", "permission_denied"})


class MarketHistoryWarmupMixin(MarketServiceState):
    def _load_tushare_history_batch(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool,
    ) -> tuple[SourceObservation, ...]:
        client = self._tushare_client
        normalized = _normalize_codes(codes)
        if client is None or not normalized:
            return ()
        trade_date = shanghai_now(observed_at).date()
        start_date = trade_date - timedelta(days=120)
        forward_adjusted = client.supports("forward_adjusted_daily")
        dataset = "forward_adjusted_daily" if forward_adjusted else "daily_history"
        adjust = "qfq" if forward_adjusted else "none"
        loader = client.fetch_forward_adjusted_daily if forward_adjusted else client.fetch_daily_history
        return self._load_tushare_reference(
            "daily_history",
            ",".join(normalized),
            {
                "dataset": dataset,
                "codes": normalized,
                "start_date": start_date.isoformat(),
                "end_date": trade_date.isoformat(),
                "adjust": adjust,
            },
            observed_at,
            loader,
            normalized,
            start_date,
            trade_date,
            observed_at,
            force=force,
        )

    def schedule_history_warmup(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> None:
        normalized = _normalize_codes(codes)
        lanes = self._source_lanes
        if not normalized or lanes is None:
            return
        now = self._monotonic()
        with self._lock:
            self._history_warmup_universe = normalized
            if self._history_warmup_inflight:
                return
            missing = tuple(
                code
                for code in normalized
                if code not in self._history_warmup_inflight
                and ((entry := self._history.get(code)) is None or entry.expires_at <= now)
            )
        if not missing:
            return

        local_seed_codes: tuple[str, ...] = ()
        available_codes = getattr(self._history_client, "available_codes", None)
        if callable(available_codes):
            try:
                local_seed_codes = tuple(available_codes(missing))
            except Exception as exc:
                _LOGGER.warning("local history seed discovery degraded: %s", type(exc).__name__)
        tushare_health = dict(self._tushare_client.health()) if self._tushare_client is not None else {}
        use_tushare = (
            not local_seed_codes
            and bool(tushare_health.get("enabled"))
            and not bool(tushare_health.get("circuit_open"))
            and tushare_health.get("degraded_reason") not in _PERMANENT_TUSHARE_DEGRADATIONS
        )
        source = "local_seed" if local_seed_codes else ("tushare" if use_tushare else "tencent")
        batch_size = self._history_warmup_batch_size
        batch = (local_seed_codes or missing)[:batch_size]
        with self._lock:
            self._history_warmup_inflight.update(batch)
            self._history_warmup_planned_count += len(batch)
            self._history_warmup_last_source = source

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
                self._load_histories,
                batch,
            )
        future.add_done_callback(lambda completed: self._finish_history_warmup(batch, completed))

    def _warm_tushare_history_batch(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> None:
        if self._tushare_client is None:
            return
        observations = self._load_tushare_history_batch(codes, observed_at, force=False)
        self._apply_tushare_history(observations)

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
        with self._lock:
            self._history_warmup_inflight.difference_update(codes)
            covered = sum(
                (entry := self._history.get(code)) is not None and entry.expires_at > now and len(entry.bars) >= 20
                for code in codes
            )
            self._history_warmup_completed_count += covered
            if not superseded:
                self._history_warmup_failure_count += max(0, len(codes) - covered)
            universe = self._history_warmup_universe
            self._history_universe_rows = len(universe)
            self._history_covered_rows = sum(
                (entry := self._history.get(code)) is not None and entry.expires_at > now and len(entry.bars) >= 20
                for code in universe
            )
        lanes = self._source_lanes
        if universe and lanes is not None and not lanes.is_stopped("history") and not lanes.is_stopped("tushare"):
            self.schedule_history_warmup(universe, self._wall_clock())
