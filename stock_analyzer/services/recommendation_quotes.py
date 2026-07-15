"""Lifecycle-owned refresh service for recommendation-pool quotes."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict, cast

import pandas as pd

from .. import config

_LOGGER = logging.getLogger(__name__)

QuoteGroup = Literal["display", "candidate"]
QuoteFetcher = Callable[[Sequence[str]], pd.DataFrame]
QuoteLoader = Callable[[], pd.DataFrame | None]
QuoteCacheWriter = Callable[[pd.DataFrame], object]
CacheClearer = Callable[[], object]
ThreadFactory = Callable[..., threading.Thread]
Clock = Callable[[], float]

_GROUPS: tuple[QuoteGroup, ...] = ("display", "candidate")


class RecommendationQuoteConfig(Protocol):
    RECOMMENDATION_CANDIDATE_POOL_SIZE: int


_DEFAULT_CONFIG = cast(RecommendationQuoteConfig, config)


class QuoteGroupStatus(TypedDict):
    running: bool
    stopping: bool
    last_success_monotonic: float
    error: str
    row_count: int
    success_count: int
    failure_count: int


class RecommendationQuoteRefreshStatus(TypedDict):
    display: QuoteGroupStatus
    candidate: QuoteGroupStatus


@dataclass
class _QuoteGroupState:
    running: bool = False
    last_started: float = 0.0
    last_success: float = 0.0
    error: str = ""
    snapshot: pd.DataFrame = field(default_factory=pd.DataFrame)
    worker: threading.Thread | None = None
    success_count: int = 0
    failure_count: int = 0


class RecommendationQuoteRefreshService:
    """Own recommendation quote snapshots, refresh workers, and cache publication."""

    def __init__(
        self,
        *,
        fetch_quotes: QuoteFetcher,
        load_full_quotes: QuoteLoader,
        cache_display_quotes: QuoteCacheWriter,
        clear_recommendation_cache: CacheClearer,
        clear_horizon_cache: CacheClearer,
        config_source: RecommendationQuoteConfig = _DEFAULT_CONFIG,
        thread_factory: ThreadFactory | None = None,
        monotonic: Clock = time.monotonic,
    ) -> None:
        self._fetch_quotes = fetch_quotes
        self._load_full_quotes = load_full_quotes
        self._cache_display_quotes = cache_display_quotes
        self._clear_recommendation_cache = clear_recommendation_cache
        self._clear_horizon_cache = clear_horizon_cache
        self._config = config_source
        self._thread_factory = thread_factory or threading.Thread
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._network_lock = threading.Lock()
        self._watched_codes: set[str] = set()
        self._states: dict[QuoteGroup, _QuoteGroupState] = {
            "display": _QuoteGroupState(),
            "candidate": _QuoteGroupState(),
        }
        self._stopping = False

    def recommendation_quotes(self, codes: Iterable[object] | None) -> tuple[pd.DataFrame, str]:
        normalized_codes = self._normalize_codes(codes)
        if not normalized_codes:
            return pd.DataFrame(), "推荐池没有股票代码"
        with self._lock:
            self._watched_codes.update(normalized_codes)
            state = self._states["display"]
            snapshot = state.snapshot.copy(deep=True)
            status = str(state.error or "")
            if state.running and not status:
                status = "推荐池行情后台刷新中"
        self._schedule("display", normalized_codes, interval_seconds=0.0)
        return snapshot, status

    def refresh_groups(self, profile: Mapping[str, object]) -> None:
        recommendation_seconds = self._seconds(profile.get("recommendation_seconds"))
        candidate_seconds = self._seconds(profile.get("candidate_seconds"))
        with self._lock:
            display_codes = sorted(self._watched_codes)
        if recommendation_seconds is not None and display_codes:
            self._schedule("display", display_codes, recommendation_seconds)
        if candidate_seconds is not None:
            candidate_codes = self._select_candidate_codes()
            if candidate_codes:
                self._schedule("candidate", candidate_codes, candidate_seconds)

    def overlay_candidate_quotes(self, quotes: pd.DataFrame) -> pd.DataFrame:
        if quotes is None or quotes.empty or "code" not in quotes.columns:
            return quotes
        with self._lock:
            candidate = self._states["candidate"].snapshot.copy(deep=True)
        if candidate.empty or "code" not in candidate.columns:
            return quotes
        result = quotes.copy(deep=True)
        update_fields = (
            "price",
            "pct_chg",
            "volume_ratio",
            "turnover_rate",
            "turnover",
            "volume",
            "amplitude",
            "high",
            "low",
            "open",
            "quote_timestamp",
            "quote_source",
        )
        candidate_index = candidate.drop_duplicates("code", keep="last").set_index("code")
        codes = result["code"].astype(str)
        for field_name in update_fields:
            if field_name not in candidate_index.columns:
                continue
            updates = codes.map(candidate_index[field_name])
            if field_name in result.columns:
                result[field_name] = updates.where(updates.notna(), result[field_name])
            else:
                result[field_name] = updates
        result.attrs.update(getattr(quotes, "attrs", {}) or {})
        result.attrs["candidate_quote_timestamp"] = str((candidate.attrs or {}).get("quote_timestamp") or "")
        return result

    def status(self) -> RecommendationQuoteRefreshStatus:
        with self._lock:
            return {
                "display": self._group_status_locked("display"),
                "candidate": self._group_status_locked("candidate"),
            }

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            workers = [state.worker for state in self._states.values() if state.worker is not None]

        deadline = self._monotonic() + max(0.0, timeout_seconds)
        for worker in workers:
            if worker is threading.current_thread() or not worker.is_alive():
                continue
            worker.join(max(0.0, deadline - self._monotonic()))

        with self._lock:
            for state in self._states.values():
                state_worker = state.worker
                if state_worker is not None and not state_worker.is_alive():
                    state.worker = None
                    state.running = False
            self._stopping = any(state.worker is not None for state in self._states.values())

    def _schedule(
        self,
        group: QuoteGroup,
        normalized_codes: Sequence[str],
        interval_seconds: float,
    ) -> bool:
        now = self._monotonic()
        with self._lock:
            state = self._states[group]
            if self._stopping or state.running:
                return False
            if state.last_started and now - state.last_started < max(0.0, interval_seconds):
                return False
            state.running = True
            state.last_started = now
            try:
                worker = self._thread_factory(
                    target=self._refresh_worker,
                    args=(group, tuple(normalized_codes)),
                    name=f"recommendation-{group}-quotes-refresh",
                    daemon=True,
                )
                state.worker = worker
                worker.start()
            except Exception as exc:
                state.running = False
                state.last_started = 0.0
                state.worker = None
                state.error = str(exc)
                state.failure_count += 1
                _LOGGER.exception("failed to start recommendation quote refresh worker: %s", group)
                return False
        return True

    def _refresh_worker(self, group: QuoteGroup, normalized_codes: Sequence[str]) -> None:
        try:
            with self._network_lock:
                fetched = self._fetch_quotes(list(normalized_codes))
            if not isinstance(fetched, pd.DataFrame):
                raise TypeError("recommendation quote provider returned a non-DataFrame result")
            snapshot = fetched.copy(deep=True)
            with self._lock:
                self._states[group].snapshot = snapshot
            if group == "display":
                self._cache_display_quotes(snapshot.copy(deep=True))
            else:
                self._clear_recommendation_cache()
                self._clear_horizon_cache()
        except Exception as exc:
            _LOGGER.exception("recommendation quote refresh failed: %s", group)
            self._finish_worker(group, error=exc)
            return
        self._finish_worker(group, snapshot=snapshot)

    def _finish_worker(
        self,
        group: QuoteGroup,
        *,
        snapshot: pd.DataFrame | None = None,
        error: Exception | None = None,
    ) -> None:
        with self._lock:
            state = self._states[group]
            if snapshot is not None:
                state.snapshot = snapshot
            if error is None and snapshot is not None:
                state.error = ""
                state.last_success = self._monotonic()
                state.success_count += 1
            elif error is not None:
                state.error = str(error)
                state.failure_count += 1
            state.running = False
            state.worker = None
            self._stopping = any(item.worker is not None for item in self._states.values())

    def _select_candidate_codes(self) -> list[str]:
        quotes = self._load_full_quotes()
        if quotes is None or quotes.empty or "code" not in quotes.columns:
            return []
        frame = quotes.copy(deep=True)
        score = pd.Series(0.0, index=frame.index)
        for column, weight in (("turnover", 0.45), ("pct_chg", 0.3), ("volume_ratio", 0.25)):
            if column in frame.columns:
                values = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
                score += values.rank(pct=True) * weight
        frame["_candidate_refresh_score"] = score
        if "price" in frame.columns:
            frame = frame[pd.to_numeric(frame["price"], errors="coerce").fillna(0.0) > 0]
        size = max(1, int(self._config.RECOMMENDATION_CANDIDATE_POOL_SIZE))
        selected = frame.nlargest(size, "_candidate_refresh_score")["code"].astype(str).tolist()
        return [str(value) for value in selected]

    def _group_status_locked(self, group: QuoteGroup) -> QuoteGroupStatus:
        state = self._states[group]
        return {
            "running": state.running,
            "stopping": self._stopping,
            "last_success_monotonic": float(state.last_success),
            "error": str(state.error or ""),
            "row_count": int(len(state.snapshot)),
            "success_count": int(state.success_count),
            "failure_count": int(state.failure_count),
        }

    @staticmethod
    def _normalize_codes(codes: Iterable[object] | None) -> list[str]:
        normalized: set[str] = set()
        for value in codes or ():
            text = str(value or "").strip()
            if len(text) >= 6:
                normalized.add(text[-6:])
        return sorted(normalized)

    @staticmethod
    def _seconds(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, (int, float, str)):
            return float(value)
        raise TypeError(f"invalid refresh interval type: {type(value).__name__}")


__all__ = [
    "QuoteGroupStatus",
    "RecommendationQuoteConfig",
    "RecommendationQuoteRefreshService",
    "RecommendationQuoteRefreshStatus",
]
