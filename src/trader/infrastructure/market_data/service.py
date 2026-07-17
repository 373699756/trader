"""Thread-safe feature-data service built from quote and history adapters."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime

from trader.domain.models import Evidence, FeatureSnapshot, MarketQuote
from trader.domain.tail import TAIL_SIGNAL_VALUE_FIELDS, MinuteBar
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar


@dataclass(frozen=True)
class _HistoryEntry:
    bars: tuple[DailyBar, ...]
    expires_at: float


@dataclass(frozen=True)
class _ResearchEntry:
    evidence: tuple[Evidence, ...]
    expires_at: float


@dataclass(frozen=True)
class _IntradayEntry:
    bars: tuple[MinuteBar, ...]
    expires_at: float


class MarketFeatureService:
    def __init__(
        self,
        gateway: MarketDataGateway,
        history_client: EastmoneyClient,
        feature_builder: FeatureBuilder,
        *,
        research_client: AkshareResearchClient | None = None,
        intraday_client: EastmoneyClient | None = None,
        history_workers: int = 6,
        research_workers: int = 4,
        intraday_workers: int = 6,
        history_preload_limit: int = 360,
        history_ttl_seconds: float = 21_600,
        research_ttl_seconds: float = 600,
        intraday_ttl_seconds: float = 45,
        intraday_batch_timeout_seconds: float = 3,
        intraday_cache_limit: int = 360,
        market_ttl_seconds: float = 30,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._history_client = history_client
        self._feature_builder = feature_builder
        self._research_client = research_client
        self._intraday_client = intraday_client
        self._history_workers = max(1, history_workers)
        self._research_workers = max(1, research_workers)
        self._intraday_workers = max(1, intraday_workers)
        self._history_preload_limit = max(1, history_preload_limit)
        self._history_ttl_seconds = max(60.0, history_ttl_seconds)
        self._research_ttl_seconds = max(60.0, research_ttl_seconds)
        self._intraday_ttl_seconds = max(1.0, intraday_ttl_seconds)
        self._intraday_batch_timeout_seconds = max(0.01, intraday_batch_timeout_seconds)
        self._intraday_cache_limit = max(1, intraday_cache_limit)
        self._market_ttl_seconds = max(1.0, market_ttl_seconds)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._market_features: tuple[FeatureSnapshot, ...] = ()
        self._market_expires_at = 0.0
        self._history: dict[str, _HistoryEntry] = {}
        self._research: dict[str, _ResearchEntry] = {}
        self._intraday: dict[str, _IntradayEntry] = {}
        self._research_success_count = 0
        self._research_error_count = 0
        self._research_last_error = ""
        self._intraday_success_count = 0
        self._intraday_error_count = 0
        self._intraday_last_error = ""
        self._intraday_requested_rows = 0
        self._intraday_covered_rows = 0
        self._intraday_latest_source_time = ""
        self._intraday_sources: tuple[str, ...] = ()
        self._intraday_data_versions: tuple[str, ...] = ()
        self._history_universe_rows = 0
        self._history_covered_rows = 0
        self._history_error_count = 0
        self._history_data_versions: tuple[str, ...] = ()

    def fetch_market_features(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        now = self._monotonic()
        with self._lock:
            if self._market_features and self._market_expires_at > now:
                return self._market_features
        quotes = tuple(self._gateway.fetch_market())
        history_codes = _history_preload_codes(quotes, self._history_preload_limit)
        histories = self._load_histories(history_codes)
        features = self._feature_builder.build(quotes, histories, observed_at)
        with self._lock:
            self._market_features = features
            self._market_expires_at = self._monotonic() + self._market_ttl_seconds
            self._history_universe_rows = len(history_codes)
            self._history_covered_rows = sum(len(histories.get(code, ())) >= 20 for code in history_codes)
            self._history_data_versions = tuple(sorted({quote.data_version for quote in quotes}))
        return features

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized:
            return ()
        quotes = tuple(self._gateway.fetch_candidates(normalized))
        received = {quote.code for quote in quotes}
        if received != set(normalized):
            market = {feature.quote.code: feature.quote for feature in self.fetch_market_features(observed_at)}
            quotes = tuple((*quotes, *(market[code] for code in normalized if code not in received and code in market)))
        histories = self._load_histories(normalized)
        research_evidence = self._load_research(normalized, observed_at)
        intraday_minutes = self._load_intraday(normalized, observed_at) if include_intraday_tail else None
        with self._lock:
            cross_section_reference = {feature.quote.code: feature.values for feature in self._market_features}
            cross_section_normalization_reference = {
                feature.quote.code: feature.normalization for feature in self._market_features
            }
        features = self._feature_builder.build(
            quotes,
            histories,
            observed_at,
            cross_section_reference=cross_section_reference,
            cross_section_normalization_reference=cross_section_normalization_reference,
            research_evidence=research_evidence,
            intraday_minutes=intraday_minutes,
        )
        if include_intraday_tail:
            self._record_intraday_feature_coverage(normalized, features)
        return features

    def health(self) -> Mapping[str, object]:
        with self._lock:
            history_entries = len(self._history)
            market_cached = len(self._market_features)
            research_entries = len(self._research)
            intraday_entries = len(self._intraday)
            research_success_count = self._research_success_count
            research_error_count = self._research_error_count
            research_last_error = self._research_last_error
            intraday_success_count = self._intraday_success_count
            intraday_error_count = self._intraday_error_count
            intraday_last_error = self._intraday_last_error
            intraday_requested_rows = self._intraday_requested_rows
            intraday_covered_rows = self._intraday_covered_rows
            intraday_latest_source_time = self._intraday_latest_source_time
            intraday_sources = self._intraday_sources
            intraday_data_versions = self._intraday_data_versions
            history_universe_rows = self._history_universe_rows
            history_covered_rows = self._history_covered_rows
            history_error_count = self._history_error_count
            history_data_versions = self._history_data_versions
        return {
            **dict(self._gateway.health()),
            "history_cache_entries": history_entries,
            "market_feature_rows": market_cached,
            "research_cache_entries": research_entries,
            "research_success_count": research_success_count,
            "research_error_count": research_error_count,
            "research_last_error": research_last_error,
            "intraday_tail_cache_entries": intraday_entries,
            "intraday_tail_success_count": intraday_success_count,
            "intraday_tail_error_count": intraday_error_count,
            "intraday_tail_last_error": intraday_last_error,
            "intraday_tail_requested_rows": intraday_requested_rows,
            "intraday_tail_covered_rows": intraday_covered_rows,
            "intraday_tail_coverage_ratio": intraday_covered_rows / intraday_requested_rows
            if intraday_requested_rows
            else 0.0,
            "intraday_tail_latest_source_time": intraday_latest_source_time,
            "intraday_tail_sources": intraday_sources,
            "intraday_tail_data_versions": intraday_data_versions,
            "history_universe_rows": history_universe_rows,
            "history_covered_rows": history_covered_rows,
            "history_coverage_ratio": history_covered_rows / history_universe_rows if history_universe_rows else 0.0,
            "history_error_count": history_error_count,
            "history_data_versions": history_data_versions,
        }

    def _load_research(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[Evidence, ...]]:
        if self._research_client is None:
            return {}
        now = self._monotonic()
        result: dict[str, tuple[Evidence, ...]] = {}
        with self._lock:
            for code in codes:
                entry = self._research.get(code)
                if entry is not None and entry.expires_at > now:
                    result[code] = entry.evidence
                elif entry is not None:
                    self._research.pop(code, None)
        missing = [code for code in codes if code not in result]
        if not missing:
            return result
        with ThreadPoolExecutor(
            max_workers=min(self._research_workers, len(missing)),
            thread_name_prefix="candidate-research",
        ) as pool:
            futures = {
                pool.submit(self._research_client.fetch_news, code, observed_at=observed_at): code for code in missing
            }
            for future in as_completed(futures):
                code = futures[future]
                ttl = self._research_ttl_seconds
                try:
                    evidence = tuple(future.result())
                except Exception as exc:
                    evidence = ()
                    ttl = min(60.0, ttl)
                    with self._lock:
                        self._research_error_count += 1
                        self._research_last_error = str(exc)[:240]
                else:
                    with self._lock:
                        self._research_success_count += 1
                result[code] = evidence
                with self._lock:
                    self._research[code] = _ResearchEntry(evidence, self._monotonic() + ttl)
        return result

    def _load_intraday(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[MinuteBar, ...]]:
        now = self._monotonic()
        result: dict[str, tuple[MinuteBar, ...]] = {}
        with self._lock:
            self._intraday_requested_rows = len(codes)
            for code in tuple(self._intraday):
                if self._intraday[code].expires_at <= now:
                    self._intraday.pop(code, None)
            for code in codes:
                entry = self._intraday.get(code)
                if entry is not None and entry.expires_at > now:
                    result[code] = entry.bars
        missing = [code for code in codes if code not in result]
        if self._intraday_client is None:
            with self._lock:
                self._intraday_covered_rows = sum(bool(result.get(code)) for code in codes)
                self._intraday_last_error = "intraday_client_unavailable"
            return result
        if missing:
            pool = ThreadPoolExecutor(
                max_workers=min(self._intraday_workers, len(missing)),
                thread_name_prefix="candidate-intraday",
            )
            try:
                futures = {
                    pool.submit(self._intraday_client.fetch_intraday_minutes, code, now=observed_at): code
                    for code in missing
                }
                completed, pending = wait(futures, timeout=self._intraday_batch_timeout_seconds)
                for future in completed:
                    code = futures[future]
                    ttl = self._intraday_ttl_seconds
                    try:
                        bars = tuple(future.result())
                    except (OSError, RuntimeError, ValueError) as exc:
                        bars = ()
                        ttl = min(15.0, ttl)
                        with self._lock:
                            self._intraday_error_count += 1
                            self._intraday_last_error = str(exc)[:240]
                    else:
                        with self._lock:
                            if bars:
                                self._intraday_success_count += 1
                            else:
                                self._intraday_error_count += 1
                                self._intraday_last_error = "empty_intraday_series"
                    result[code] = bars
                    with self._lock:
                        self._intraday[code] = _IntradayEntry(bars, self._monotonic() + ttl)
                for future in pending:
                    code = futures[future]
                    future.cancel()
                    result[code] = ()
                    with self._lock:
                        self._intraday_error_count += 1
                        self._intraday_last_error = "intraday_batch_deadline"
                        self._intraday[code] = _IntradayEntry(
                            (),
                            self._monotonic() + min(15.0, self._intraday_ttl_seconds),
                        )
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            self._intraday_covered_rows = sum(bool(result.get(code)) for code in codes)
            bars = tuple(bar for code in codes for bar in result.get(code, ()))
            self._intraday_latest_source_time = max(
                (bar.source_time.isoformat() for bar in bars),
                default="",
            )
            self._intraday_sources = tuple(sorted({bar.source for bar in bars if bar.source}))
            self._intraday_data_versions = tuple(sorted({bar.data_version for bar in bars if bar.data_version}))
            excess = len(self._intraday) - self._intraday_cache_limit
            if excess > 0:
                requested = set(codes)
                oldest = sorted(
                    self._intraday,
                    key=lambda code: (code in requested, self._intraday[code].expires_at, code),
                )[:excess]
                for code in oldest:
                    self._intraday.pop(code, None)
            if self._intraday_covered_rows == self._intraday_requested_rows:
                self._intraday_last_error = ""
        return result

    def _record_intraday_feature_coverage(
        self,
        codes: Sequence[str],
        features: Sequence[FeatureSnapshot],
    ) -> None:
        covered_codes = {
            feature.quote.code
            for feature in features
            if all(feature.optional_value(field) is not None for field in TAIL_SIGNAL_VALUE_FIELDS)
        }
        covered_rows = sum(code in covered_codes for code in codes)
        with self._lock:
            self._intraday_requested_rows = len(codes)
            self._intraday_covered_rows = covered_rows
            if covered_rows == len(codes):
                self._intraday_last_error = ""
            elif not self._intraday_last_error:
                self._intraday_last_error = "intraday_series_incomplete"

    def _load_histories(self, codes: Sequence[str]) -> Mapping[str, tuple[DailyBar, ...]]:
        result = self._cached_histories(codes)
        missing = [code for code in codes if code not in result]
        if not missing:
            return result
        with ThreadPoolExecutor(
            max_workers=min(self._history_workers, len(missing)),
            thread_name_prefix="candidate-history",
        ) as pool:
            futures = {pool.submit(self._history_client.fetch_history, code, days=90): code for code in missing}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    bars = tuple(future.result())
                except Exception:
                    bars = ()
                    with self._lock:
                        self._history_error_count += 1
                result[code] = bars
                if bars:
                    with self._lock:
                        self._history[code] = _HistoryEntry(
                            bars=bars,
                            expires_at=self._monotonic() + self._history_ttl_seconds,
                        )
                else:
                    with self._lock:
                        self._history[code] = _HistoryEntry(
                            bars=(),
                            expires_at=self._monotonic() + min(60.0, self._history_ttl_seconds),
                        )
        return result

    def _cached_histories(self, codes: Iterable[str]) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(codes)
        now = self._monotonic()
        result: dict[str, tuple[DailyBar, ...]] = {}
        with self._lock:
            for code in requested:
                entry = self._history.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    self._history.pop(code, None)
                    continue
                result[code] = entry.bars
        return result


def _history_preload_codes(quotes: Sequence[MarketQuote], limit: int) -> tuple[str, ...]:
    groups: dict[str, list[MarketQuote]] = {}
    for quote in quotes:
        if quote.is_suspended or quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
            continue
        groups.setdefault(quote.industry or "unknown", []).append(quote)
    for group in groups.values():
        group.sort(key=_history_priority)
    representatives = sorted((group[0] for group in groups.values()), key=_history_priority)
    selected = representatives[:limit]
    selected_codes = {quote.code for quote in selected}
    remaining = sorted(
        (quote for group in groups.values() for quote in group if quote.code not in selected_codes),
        key=_history_priority,
    )
    selected.extend(remaining[: max(0, limit - len(selected))])
    return tuple(quote.code for quote in selected)


def _history_priority(quote: MarketQuote) -> tuple[float, float, str]:
    return (
        -(quote.amount if quote.amount is not None and math.isfinite(quote.amount) else -1.0),
        -(abs(quote.pct_change) if quote.pct_change is not None and math.isfinite(quote.pct_change) else -1.0),
        quote.code,
    )


__all__ = ["MarketFeatureService"]
