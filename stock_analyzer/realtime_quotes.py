"""Realtime quote acquisition with explicit state and dependency ownership."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypedDict, cast

import pandas as pd

from . import config
from .runtime_json import atomic_write_text

_LOGGER = logging.getLogger(__name__)

FrameFetcher = Callable[[], pd.DataFrame]
RecommendationCodes = Iterable[object] | None
RecommendationFetcher = Callable[[RecommendationCodes], pd.DataFrame]
ThreadFactory = Callable[..., threading.Thread]
CodeNormalizer = Callable[[object], str]
FrameNormalizer = Callable[[pd.DataFrame], pd.DataFrame]


class RealtimeQuoteConfig(Protocol):
    QUOTE_SNAPSHOT_MIN_ROWS: int
    QUOTE_SNAPSHOT_PATH: str


_DEFAULT_CONFIG = cast(RealtimeQuoteConfig, config)


@dataclass
class ProviderStatus:
    quotes_source: str = "unavailable"
    sentiment_source: str = "unavailable"
    last_quote_refresh: str | None = None
    last_sentiment_refresh: str | None = None
    last_quote_latency_ms: float | None = None
    quote_fetch_count: int = 0
    quote_fetch_success_count: int = 0
    quote_fetch_error_count: int = 0
    quote_last_error: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class QuoteFetchers:
    """External quote functions injected at the application composition boundary."""

    eastmoney: FrameFetcher
    sina: FrameFetcher
    tushare: FrameFetcher
    recommendation: RecommendationFetcher
    normalize_code: CodeNormalizer
    normalize_columns: FrameNormalizer


@dataclass
class _QuoteRefreshState:
    running: bool = False
    stopping: bool = False
    last_started_at: str = ""
    last_finished_at: str = ""
    last_success_at: str = ""
    last_error: str = ""
    last_started_ts: float = 0.0


class QuoteRefreshStatus(TypedDict):
    running: bool
    stopping: bool
    last_started_at: str
    last_finished_at: str
    last_success_at: str
    last_error: str


class QuoteSnapshotStore:
    """Persist and validate the last complete full-market quote snapshot."""

    def __init__(
        self,
        config_source: RealtimeQuoteConfig = _DEFAULT_CONFIG,
    ) -> None:
        self._config = config_source

    def save(self, frame: pd.DataFrame) -> None:
        minimum_rows = int(self._config.QUOTE_SNAPSHOT_MIN_ROWS)
        if frame is None or frame.empty or len(frame) < minimum_rows:
            return
        path = Path(str(self._config.QUOTE_SNAPSHOT_PATH))
        try:
            snapshot = frame.copy()
            quote_timestamp = str((frame.attrs or {}).get("quote_timestamp") or "").strip()
            if quote_timestamp:
                snapshot["__quote_timestamp"] = quote_timestamp
            atomic_write_text(str(path), snapshot.to_json(orient="records", force_ascii=False))
        except Exception:
            _LOGGER.exception("failed to persist realtime quote snapshot")

    def load(self) -> pd.DataFrame | None:
        path = Path(str(self._config.QUOTE_SNAPSHOT_PATH))
        try:
            if not path.exists():
                return None
            stat = path.stat()
            age_seconds = time.time() - stat.st_mtime
            max_age_seconds = int(getattr(self._config, "QUOTE_SNAPSHOT_MAX_AGE_SECONDS", 21600))
            now = datetime.now()
            clock = now.strftime("%H:%M")
            if now.weekday() < 5 and ("09:15" <= clock <= "11:35" or "13:00" <= clock <= "15:10"):
                intraday_max_age = max(
                    30,
                    int(getattr(self._config, "QUOTE_SNAPSHOT_INTRADAY_MAX_AGE_SECONDS", 90)),
                )
                max_age_seconds = min(max_age_seconds, intraday_max_age)
            if age_seconds > max_age_seconds:
                return None
            frame = pd.read_json(path)
        except Exception:
            return None

        minimum_rows = int(self._config.QUOTE_SNAPSHOT_MIN_ROWS)
        if frame.empty or len(frame) < minimum_rows:
            return None
        if "__quote_timestamp" in frame.columns:
            timestamps = [
                str(value).strip() for value in frame["__quote_timestamp"].dropna().tolist() if str(value).strip()
            ]
            if timestamps:
                frame.attrs["quote_timestamp"] = timestamps[0]
            frame = frame.drop(columns=["__quote_timestamp"])
        frame.attrs["snapshot_mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        return frame


class RealtimeQuoteProvider:
    """Own realtime quote fallback, status, snapshot, and refresh lifecycle."""

    def __init__(
        self,
        fetchers: QuoteFetchers,
        *,
        web_nonblocking: bool = False,
        snapshot_store: QuoteSnapshotStore | None = None,
        config_source: RealtimeQuoteConfig = _DEFAULT_CONFIG,
        thread_factory: ThreadFactory | None = None,
    ) -> None:
        self.fetchers = fetchers
        self.status = ProviderStatus()
        self._web_nonblocking = bool(web_nonblocking)
        self.snapshot_store = snapshot_store or QuoteSnapshotStore(config_source)
        self._config = config_source
        self._thread_factory = thread_factory or threading.Thread
        self._status_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refresh_state = _QuoteRefreshState()
        self._refresh_thread: threading.Thread | None = None

    def get_realtime_quotes(self) -> pd.DataFrame:
        if self._web_nonblocking:
            return self.get_web_realtime_quotes()

        errors: list[str] = []
        frame = self._fetch_live_quotes(errors)
        if frame is not None:
            return frame

        snapshot = self.snapshot_store.load()
        if snapshot is not None and not snapshot.empty:
            refresh_time = snapshot.attrs.get("snapshot_mtime") or datetime.now().isoformat(timespec="seconds")
            self._set_quote_source("本地快照", str(refresh_time))
            self._set_status_errors(errors)
            return snapshot

        self._set_quote_source("unavailable", datetime.now().isoformat(timespec="seconds"))
        self._set_status_errors(errors)
        raise RuntimeError("; ".join(errors))

    def get_web_realtime_quotes(self) -> pd.DataFrame:
        """Return a local snapshot immediately and refresh remote data off-request."""
        snapshot = self.snapshot_store.load()
        self.refresh_async()
        if snapshot is not None and not snapshot.empty:
            refresh_time = snapshot.attrs.get("snapshot_mtime") or datetime.now().isoformat(timespec="seconds")
            self._set_quote_source("本地快照", str(refresh_time))
            return snapshot
        message = "实时行情正在后台刷新，Web 请求未等待行情下载"
        self._set_quote_source("后台刷新中", datetime.now().isoformat(timespec="seconds"))
        self.append_error(message)
        raise RuntimeError(message)

    def get_recommendation_quotes(self, codes: RecommendationCodes) -> pd.DataFrame:
        started_at = time.perf_counter()
        try:
            raw = self.fetchers.recommendation(codes)
            quote_timestamp = str((raw.attrs or {}).get("quote_timestamp") or "")
            frame = self.fetchers.normalize_columns(raw)
            timestamp_by_code = {
                self.fetchers.normalize_code(row.get("代码")): str(row.get("时间戳") or "")
                for row in raw.to_dict(orient="records")
            }
            frame["quote_timestamp"] = frame["code"].map(timestamp_by_code).fillna(quote_timestamp)
            frame["quote_source"] = "腾讯推荐池批量行情"
            frame.attrs["quote_timestamp"] = quote_timestamp
            frame.attrs["quote_source"] = "腾讯推荐池批量行情"
            requested = {
                self.fetchers.normalize_code(code) for code in (codes or []) if self.fetchers.normalize_code(code)
            }
            received = set(frame["code"].astype(str)) if "code" in frame.columns else set()
            frame.attrs["missing_codes"] = sorted(requested - received)
            frame.attrs["coverage_ratio"] = round(len(received & requested) / max(1, len(requested)), 4)
        except Exception as exc:
            self._record_fetch_result(
                source="腾讯推荐池批量行情",
                success=False,
                latency_ms=self._elapsed_ms(started_at),
                error=str(exc),
            )
            raise

        self._record_fetch_result(
            source="腾讯推荐池批量行情",
            success=True,
            latency_ms=self._elapsed_ms(started_at),
        )
        return frame

    def refresh_async(self, force: bool = False) -> bool:
        now_ts = time.time()
        min_interval = max(
            30,
            int(getattr(self._config, "QUOTE_BACKGROUND_REFRESH_INTERVAL_SECONDS", 300)),
        )
        with self._refresh_lock:
            state = self._refresh_state
            if state.running or state.stopping:
                return False
            if not force and state.last_started_ts and now_ts - state.last_started_ts < min_interval:
                return False
            state.running = True
            state.last_started_ts = now_ts
            state.last_started_at = datetime.now().isoformat(timespec="seconds")
            state.last_error = ""
            try:
                worker = self._thread_factory(
                    target=self._refresh_worker,
                    name="market-quotes-background-refresh",
                    daemon=True,
                )
                self._refresh_thread = worker
                worker.start()
            except Exception as exc:
                self._record_refresh_start_failure_locked(exc)
                return False
        return True

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._refresh_lock:
            self._refresh_state.stopping = True
            worker = self._refresh_thread
            if worker is None:
                self._refresh_state.running = False
                self._refresh_state.stopping = False
                return
        if worker is not threading.current_thread() and worker.is_alive():
            worker.join(max(0.0, timeout_seconds))
        with self._refresh_lock:
            if self._refresh_thread is None:
                self._refresh_state.running = False
                self._refresh_state.stopping = False
            elif self._refresh_thread is worker and not worker.is_alive():
                self._refresh_state.running = False
                self._refresh_thread = None
                self._refresh_state.stopping = False

    def refresh_status(self) -> QuoteRefreshStatus:
        with self._refresh_lock:
            state = self._refresh_state
            return {
                "running": state.running,
                "stopping": state.stopping,
                "last_started_at": state.last_started_at,
                "last_finished_at": state.last_finished_at,
                "last_success_at": state.last_success_at,
                "last_error": state.last_error,
            }

    def health(self) -> dict[str, object]:
        with self._status_lock:
            status = {
                "quotes_source": self.status.quotes_source,
                "sentiment_source": self.status.sentiment_source,
                "last_quote_refresh": self.status.last_quote_refresh,
                "last_sentiment_refresh": self.status.last_sentiment_refresh,
                "last_quote_latency_ms": self.status.last_quote_latency_ms,
                "quote_fetch_count": self.status.quote_fetch_count,
                "quote_fetch_success_count": self.status.quote_fetch_success_count,
                "quote_fetch_error_count": self.status.quote_fetch_error_count,
                "quote_last_error": self.status.quote_last_error,
                "errors": list(self.status.errors[-10:]),
            }
        return {**status, "quote_background_refresh": self.refresh_status()}

    def append_error(self, message: str) -> None:
        with self._status_lock:
            self.status.errors.append(message)
            self.status.errors = self.status.errors[-20:]

    def record_sentiment_refresh(self, source: str) -> None:
        with self._status_lock:
            self.status.sentiment_source = source
            self.status.last_sentiment_refresh = datetime.now().isoformat(timespec="seconds")

    def save_snapshot(self, frame: pd.DataFrame) -> None:
        self.snapshot_store.save(frame)

    def load_snapshot(self) -> pd.DataFrame | None:
        return self.snapshot_store.load()

    def run_refresh_worker(self) -> None:
        """Compatibility entry point for synchronous tests and maintenance tools."""
        self._refresh_worker()

    def _fetch_live_quotes(self, errors: list[str]) -> pd.DataFrame | None:
        sources: list[tuple[str, str, FrameFetcher]] = [
            ("东方财富直连", "东方财富直连行情失败", self.fetchers.eastmoney),
        ]
        if bool(getattr(self._config, "ALLOW_SLOW_QUOTE_FALLBACK", False)):
            sources.append(("新浪并发行情", "新浪行情失败", self.fetchers.sina))
            if str(getattr(self._config, "TUSHARE_TOKEN", "")).strip():
                sources.append(("Tushare", "Tushare 行情失败", self.fetchers.tushare))

        for source, error_prefix, fetcher in sources:
            started_at = time.perf_counter()
            try:
                frame = fetcher()
            except Exception as exc:  # pragma: no cover - remote failures vary by provider
                self._record_fetch_result(
                    source=source,
                    success=False,
                    latency_ms=self._elapsed_ms(started_at),
                    error=str(exc),
                )
                errors.append(f"{error_prefix}: {exc}")
                continue
            return self._accept_live_quotes(frame, source, errors, self._elapsed_ms(started_at))
        return None

    def _accept_live_quotes(
        self,
        frame: pd.DataFrame,
        source: str,
        errors: list[str],
        latency_ms: float,
    ) -> pd.DataFrame:
        refresh_time = datetime.now().isoformat(timespec="seconds")
        frame.attrs.setdefault("quote_timestamp", refresh_time)
        with self._status_lock:
            self.status.quotes_source = source
            self.status.last_quote_refresh = refresh_time
            self.status.errors = list(errors)[-20:]
            self.status.quote_fetch_count += 1
            self.status.quote_fetch_success_count += 1
            self.status.last_quote_latency_ms = latency_ms
            self.status.quote_last_error = ""
        self.snapshot_store.save(frame)
        return frame

    def _refresh_worker(self) -> None:
        errors = self._status_errors()
        error = ""
        success = False
        try:
            success = self._fetch_live_quotes(errors) is not None
            if not success:
                error = "; ".join(errors[-3:]) or "后台行情刷新没有可用数据源"
                self._set_status_errors(errors)
        except Exception as exc:
            error = str(exc)
            _LOGGER.exception("行情刷新任务异常: %s", error)
        finished_at = datetime.now().isoformat(timespec="seconds")
        with self._refresh_lock:
            state = self._refresh_state
            state.running = False
            state.last_finished_at = finished_at
            state.last_error = error
            if success:
                state.last_success_at = finished_at
            state.stopping = False
            self._refresh_thread = None

    def _record_fetch_result(
        self,
        *,
        source: str,
        success: bool,
        latency_ms: float | None,
        error: str = "",
    ) -> None:
        with self._status_lock:
            self.status.quote_fetch_count += 1
            self.status.last_quote_latency_ms = latency_ms
            if success:
                self.status.quote_fetch_success_count += 1
                self.status.quote_last_error = ""
                self.status.quotes_source = source
            else:
                self.status.quote_fetch_error_count += 1
                self.status.quote_last_error = error

    def _record_refresh_start_failure_locked(self, exc: Exception) -> None:
        state = self._refresh_state
        state.running = False
        state.stopping = False
        state.last_finished_at = datetime.now().isoformat(timespec="seconds")
        state.last_error = str(exc)
        self._refresh_thread = None

    def _set_quote_source(self, source: str, refresh_time: str) -> None:
        with self._status_lock:
            self.status.quotes_source = source
            self.status.last_quote_refresh = refresh_time

    def _set_status_errors(self, messages: list[str]) -> None:
        with self._status_lock:
            self.status.errors = list(messages)[-20:]

    def _status_errors(self) -> list[str]:
        with self._status_lock:
            return list(self.status.errors)

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return max(0.0, (time.perf_counter() - started_at) * 1000.0)


__all__ = [
    "ProviderStatus",
    "QuoteFetchers",
    "QuoteRefreshStatus",
    "QuoteSnapshotStore",
    "RealtimeQuoteConfig",
    "RealtimeQuoteProvider",
]
