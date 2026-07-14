import math
import re
import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from . import config
from .daily_data import load_execution_history_frames, load_history_frames
from .history_cache import HistoryCache
from .normalization import normalize_code, rename_known_columns
from .runtime_json import atomic_write_text

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProviderStatus:
    quotes_source: str = "unavailable"
    sentiment_source: str = "unavailable"
    last_quote_refresh: Optional[str] = None
    last_sentiment_refresh: Optional[str] = None
    last_quote_latency_ms: Optional[float] = None
    quote_fetch_count: int = 0
    quote_fetch_success_count: int = 0
    quote_fetch_error_count: int = 0
    quote_last_error: str = ""
    errors: List[str] = field(default_factory=list)


class MarketDataProvider:
    def __init__(self, web_nonblocking: bool = False) -> None:
        self.status = ProviderStatus()
        self._web_nonblocking = bool(web_nonblocking)
        self._akshare = None
        self._tushare = None
        self._tushare_api = None
        self._status_lock = threading.Lock()
        self._quote_refresh_lock = threading.Lock()
        self._quote_refresh_running = False
        self._quote_refresh_last_started_at = ""
        self._quote_refresh_last_finished_at = ""
        self._quote_refresh_last_success_at = ""
        self._quote_refresh_last_error = ""
        self._quote_refresh_last_started_ts = 0.0
        self._history_cache = HistoryCache(
            config.HISTORY_CACHE_PATH,
            freshness_hours=config.HISTORY_CACHE_FRESHNESS_HOURS,
        )

    def _record_quote_fetch_result(self, *, source: str, success: bool, latency_ms: Optional[float], error: str = "") -> None:
        with self._status_lock:
            self.status.quote_fetch_count += 1
            self.status.last_quote_latency_ms = latency_ms
            if success:
                self.status.quote_fetch_success_count += 1
                self.status.quote_last_error = ""
            else:
                self.status.quote_fetch_error_count += 1
                self.status.quote_last_error = error
            self.status.quotes_source = source if success else self.status.quotes_source

    def _append_status_error(self, message: str) -> None:
        with self._status_lock:
            self.status.errors.append(message)
            self.status.errors = self.status.errors[-20:]

    def _set_status_errors(self, messages: List[str]) -> None:
        with self._status_lock:
            self.status.errors = list(messages)[-20:]

    def _set_quote_source(self, source: str, refresh_time: str) -> None:
        with self._status_lock:
            self.status.quotes_source = source
            self.status.last_quote_refresh = refresh_time

    def _record_quote_latency(self, source: str, start_time: float) -> None:
        latency_ms = max(0.0, (time.perf_counter() - start_time) * 1000.0)
        self._record_quote_fetch_result(source=source, success=True, latency_ms=latency_ms)

    def get_realtime_quotes(self) -> pd.DataFrame:
        if self._web_nonblocking:
            return self.get_web_realtime_quotes()
        errors = []
        start_time = time.perf_counter()
        try:
            df = self._fetch_eastmoney_quotes()
            self._record_quote_latency("东方财富直连", start_time)
            return self._accept_realtime_quotes(df, "东方财富直连", errors)
        except Exception as exc:  # pragma: no cover - depends on remote services
            self._record_quote_fetch_result(
                source="东方财富直连",
                success=False,
                latency_ms=None,
                error=str(exc),
            )
            errors.append("东方财富直连行情失败: {}".format(exc))

        if config.ALLOW_SLOW_QUOTE_FALLBACK:
            try:
                start_time = time.perf_counter()
                df = self._fetch_sina_quotes()
                self._record_quote_latency("新浪并发行情", start_time)
                return self._accept_realtime_quotes(df, "新浪并发行情", errors)
            except Exception as exc:  # pragma: no cover - depends on remote services
                self._record_quote_fetch_result(
                    source="新浪并发行情",
                    success=False,
                    latency_ms=None,
                    error=str(exc),
                )
                errors.append("新浪行情失败: {}".format(exc))

            if config.TUSHARE_TOKEN:
                try:
                    start_time = time.perf_counter()
                    df = self._fetch_tushare_quotes()
                    self._record_quote_latency("Tushare", start_time)
                    return self._accept_realtime_quotes(df, "Tushare", errors)
                except Exception as exc:  # pragma: no cover - depends on remote services
                    self._record_quote_fetch_result(
                        source="Tushare",
                        success=False,
                        latency_ms=None,
                        error=str(exc),
                    )
                    errors.append("Tushare 行情失败: {}".format(exc))

        snapshot = self._load_quote_snapshot()
        if snapshot is not None and not snapshot.empty:
            refresh_time = snapshot.attrs.get("snapshot_mtime") or datetime.now().isoformat(timespec="seconds")
            self._set_quote_source("本地快照", refresh_time)
            self._set_status_errors(errors)
            return snapshot

        self._set_quote_source("unavailable", datetime.now().isoformat(timespec="seconds"))
        self._set_status_errors(errors)
        raise RuntimeError("; ".join(errors))

    def get_web_realtime_quotes(self) -> pd.DataFrame:
        """Serve a local snapshot immediately; perform all remote work in background."""
        snapshot = self._load_quote_snapshot()
        self.refresh_realtime_quotes_async()
        if snapshot is not None and not snapshot.empty:
            refresh_time = snapshot.attrs.get("snapshot_mtime") or datetime.now().isoformat(timespec="seconds")
            self._set_quote_source("本地快照", refresh_time)
            return snapshot
        message = "实时行情正在后台刷新，Web 请求未等待行情下载"
        self._set_quote_source("后台刷新中", datetime.now().isoformat(timespec="seconds"))
        self._append_status_error(message)
        raise RuntimeError(message)

    def refresh_realtime_quotes_async(self, force: bool = False) -> bool:
        now = time.time()
        min_interval = max(30, int(getattr(config, "QUOTE_BACKGROUND_REFRESH_INTERVAL_SECONDS", 300)))
        with self._quote_refresh_lock:
            if self._quote_refresh_running:
                return False
            if not force and self._quote_refresh_last_started_ts and now - self._quote_refresh_last_started_ts < min_interval:
                return False
            self._quote_refresh_running = True
            self._quote_refresh_last_started_ts = now
            self._quote_refresh_last_started_at = datetime.now().isoformat(timespec="seconds")
            self._quote_refresh_last_error = ""
        worker = threading.Thread(
            target=self._refresh_realtime_quotes_worker,
            name="market-quotes-background-refresh",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            with self._quote_refresh_lock:
                self._quote_refresh_running = False
                self._quote_refresh_last_finished_at = datetime.now().isoformat(timespec="seconds")
                self._quote_refresh_last_error = str(exc)
            return False
        return True

    def quote_refresh_status(self) -> Dict[str, object]:
        with self._quote_refresh_lock:
            return {
                "running": self._quote_refresh_running,
                "last_started_at": self._quote_refresh_last_started_at,
                "last_finished_at": self._quote_refresh_last_finished_at,
                "last_success_at": self._quote_refresh_last_success_at,
                "last_error": self._quote_refresh_last_error,
            }

    def _refresh_realtime_quotes_worker(self) -> None:
        error = ""
        success = False
        errors = list(self.status.errors)
        try:
            try:
                start_time = time.perf_counter()
                quotes = self._fetch_eastmoney_quotes()
                self._record_quote_latency("东方财富直连", start_time)
                self._accept_realtime_quotes(quotes, "东方财富直连", errors)
                success = True
            except Exception as exc:  # pragma: no cover - depends on remote services
                self._record_quote_fetch_result(
                    source="东方财富直连",
                    success=False,
                    latency_ms=None,
                    error=str(exc),
                )
                errors.append("东方财富直连行情失败: {}".format(exc))
            if not success and config.ALLOW_SLOW_QUOTE_FALLBACK:
                try:
                    start_time = time.perf_counter()
                    quotes = self._fetch_sina_quotes()
                    self._record_quote_latency("新浪并发行情", start_time)
                    self._accept_realtime_quotes(quotes, "新浪并发行情", errors)
                    success = True
                except Exception as exc:  # pragma: no cover - depends on remote services
                    self._record_quote_fetch_result(
                        source="新浪并发行情",
                        success=False,
                        latency_ms=None,
                        error=str(exc),
                    )
                    errors.append("新浪行情失败: {}".format(exc))
            if not success and config.TUSHARE_TOKEN:
                try:
                    start_time = time.perf_counter()
                    quotes = self._fetch_tushare_quotes()
                    self._record_quote_latency("Tushare", start_time)
                    self._accept_realtime_quotes(quotes, "Tushare", errors)
                    success = True
                except Exception as exc:  # pragma: no cover - depends on remote services
                    self._record_quote_fetch_result(
                        source="Tushare",
                        success=False,
                        latency_ms=None,
                        error=str(exc),
                    )
                    errors.append("Tushare 行情失败: {}".format(exc))
            if not success:
                error = "; ".join(errors[-3:]) or "后台行情刷新没有可用数据源"
                self._set_status_errors(errors)
        except Exception as exc:
            error = str(exc)
            _LOGGER.exception("行情刷新任务异常: %s", error)
        finished_at = datetime.now().isoformat(timespec="seconds")
        with self._quote_refresh_lock:
            self._quote_refresh_running = False
            self._quote_refresh_last_finished_at = finished_at
            self._quote_refresh_last_error = error
            if success:
                self._quote_refresh_last_success_at = finished_at

    def _accept_realtime_quotes(self, df: pd.DataFrame, source: str, errors: List[str]) -> pd.DataFrame:
        refresh_time = datetime.now().isoformat(timespec="seconds")
        df.attrs.setdefault("quote_timestamp", refresh_time)
        self._set_quote_source(source, refresh_time)
        self._set_status_errors(errors)
        self._record_quote_fetch_result(source=source, success=True, latency_ms=None, error="")
        self._save_quote_snapshot(df)
        return df

    def get_hot_ranks(self) -> Dict[str, int]:
        ak = self._get_akshare()
        rank_map: Dict[str, int] = {}
        for func_name in ("stock_hot_rank_em", "stock_hot_up_em"):
            try:
                func = getattr(ak, func_name)
                df = func()
            except Exception:  # pragma: no cover - optional remote signal
                continue
            if df is None or df.empty:
                continue
            for index, row in df.reset_index(drop=True).iterrows():
                code_value = None
                for col in ("代码", "股票代码", "code"):
                    if col in row:
                        code_value = row[col]
                        break
                if code_value is None:
                    continue
                code = normalize_code(code_value)
                rank_map[code] = min(rank_map.get(code, 9999), index + 1)
        return rank_map

    def get_industry_strength(self) -> Dict[str, float]:
        ak = self._get_akshare()
        try:
            df = ak.stock_board_industry_name_em()
        except Exception:  # pragma: no cover - optional remote signal
            return {}
        if df is None or df.empty:
            return {}
        strength: Dict[str, float] = {}
        for _, row in df.iterrows():
            name = ""
            for col in ("板块名称", "名称", "行业"):
                if col in row and pd.notna(row[col]):
                    name = str(row[col]).strip()
                    break
            if not name:
                continue
            pct = 0.0
            for col in ("涨跌幅", "涨幅", "最新涨跌幅"):
                if col in row:
                    pct = _to_float(row[col])
                    break
            strength[name] = pct
        return strength

    def get_stock_news(self, code: str, name: str = "", limit: int = 20) -> List[Dict[str, str]]:
        ak = self._get_akshare()
        news: List[Dict[str, str]] = []
        symbol = code
        try:
            df = ak.stock_news_em(symbol=symbol)
            news.extend(_extract_news_rows(df, "东方财富个股新闻", limit))
        except Exception as exc:  # pragma: no cover - optional remote signal
            self._record_sentiment_error("个股新闻失败 {}: {}".format(code, exc))

        for func_name, source in (
            ("stock_info_global_cls", "财联社电报"),
            ("stock_news_main_cx", "财新精选"),
        ):
            try:
                func = getattr(ak, func_name)
                df = func()
            except Exception as exc:  # pragma: no cover - optional remote signal
                self._record_sentiment_error("{}失败: {}".format(source, exc))
                continue
            if df is None or df.empty:
                continue
            news.extend(_extract_news_rows(df, source, limit, keyword=name or code))

        self.status.sentiment_source = "AKShare 新闻/电报"
        self.status.last_sentiment_refresh = datetime.now().isoformat(timespec="seconds")
        return news[:limit]

    def get_market_news(self, limit: int = 100) -> List[Dict[str, str]]:
        ak = self._get_akshare()
        news: List[Dict[str, str]] = []
        for func_name, source in (
            ("stock_info_global_cls", "财联社电报"),
            ("stock_news_main_cx", "财新精选"),
        ):
            try:
                func = getattr(ak, func_name)
                df = func()
            except Exception as exc:  # pragma: no cover - optional remote signal
                self._record_sentiment_error("{}失败: {}".format(source, exc))
                continue
            news.extend(_extract_news_rows(df, source, limit))
        self.status.sentiment_source = "AKShare 新闻/电报"
        self.status.last_sentiment_refresh = datetime.now().isoformat(timespec="seconds")
        return news[:limit]

    def get_share_unlock_events(self) -> List[Dict[str, object]]:
        try:
            ak = self._get_akshare()
            for func_name in ("stock_restricted_release_summary_em", "stock_restricted_release_queue_em"):
                try:
                    df = getattr(ak, func_name)()
                except Exception:
                    continue
                rows = _event_rows(
                    df,
                    code_keys=("代码", "股票代码", "证券代码"),
                    date_keys=("解禁日期", "上市日期"),
                    ratio_keys=("解禁比例", "解禁占比", "占总股本比例", "解禁数量占总股本比例"),
                )
                if rows:
                    return rows
        except Exception as exc:  # pragma: no cover - optional remote signal
            self._record_sentiment_error("解禁数据失败: {}".format(exc))
        return []

    def get_pledge_risk(self) -> List[Dict[str, object]]:
        if not config.TUSHARE_TOKEN:
            return []
        try:
            pro = self._get_tushare_api(config.TUSHARE_TOKEN)
            df = pro.pledge_stat()
            return _event_rows(df, code_keys=("ts_code", "股票代码", "证券代码"), ratio_keys=("pledge_ratio", "质押比例"))
        except Exception as exc:  # pragma: no cover - optional remote signal
            self._record_sentiment_error("质押数据失败: {}".format(exc))
            return []

    def get_reduction_plans(self) -> List[Dict[str, object]]:
        try:
            ak = self._get_akshare()
            for func_name in ("stock_share_reduce_holdings_cninfo", "stock_hold_control_cninfo"):
                try:
                    df = getattr(ak, func_name)()
                except Exception:
                    continue
                rows = _event_rows(df, code_keys=("证券代码", "股票代码", "代码"), date_keys=("公告日期", "变动日期"))
                if rows:
                    return rows
        except Exception as exc:  # pragma: no cover - optional remote signal
            self._record_sentiment_error("减持数据失败: {}".format(exc))
        return []

    def get_financial_calendar(self) -> List[Dict[str, object]]:
        try:
            ak = self._get_akshare()
            for func_name in ("stock_yjbb_em", "stock_yjyg_em"):
                try:
                    df = getattr(ak, func_name)()
                except Exception:
                    continue
                rows = _event_rows(df, code_keys=("股票代码", "代码", "证券代码"), date_keys=("公告日期", "预约披露日期"))
                if rows:
                    return rows
        except Exception as exc:  # pragma: no cover - optional remote signal
            self._record_sentiment_error("财报日历失败: {}".format(exc))
        return []

    def get_fundamental_factors(self, codes: List[str] = None) -> Dict[str, Dict[str, object]]:
        codes = [normalize_code(code) for code in (codes or []) if normalize_code(code)]
        codes = codes[: max(1, int(getattr(config, "FUNDAMENTAL_FETCH_LIMIT", 200)))]
        if not codes:
            return {}
        items: Dict[str, Dict[str, object]] = {}
        if config.TUSHARE_TOKEN:
            try:
                pro = self._get_tushare_api(config.TUSHARE_TOKEN)
                for code in codes:
                    try:
                        df = pro.fina_indicator(ts_code=_to_ts_code(code), limit=1)
                    except Exception:
                        continue
                    _merge_fundamental_item(items, code, df)
            except Exception as exc:  # pragma: no cover - optional remote signal
                self._record_sentiment_error("Tushare 财务指标失败: {}".format(exc))
        try:
            ak = self._get_akshare()
            for code in codes:
                if code in items and items[code].get("roe"):
                    continue
                try:
                    df = ak.stock_financial_analysis_indicator(symbol=code)
                except Exception:
                    continue
                _merge_fundamental_item(items, code, df)
        except Exception as exc:  # pragma: no cover - optional remote signal
            self._record_sentiment_error("AKShare 财务指标失败: {}".format(exc))
        return items

    def get_history(self, code: str, days: int = 90) -> pd.DataFrame:
        normalized = normalize_code(code)
        cached, cache_fresh = self._read_history_cache(normalized, days)
        if _usable_history(cached, days) and cache_fresh:
            return self._with_price_metadata(cached, price_adjustment_mode="adjusted", data_source="cache")
        local = self._load_local_history(normalized, days)
        if _usable_history(local, days):
            return self._with_price_metadata(local, price_adjustment_mode="adjusted", data_source="local")
        try:
            fetched = self._fetch_akshare_history(normalized, days)
        except Exception as exc:  # pragma: no cover - depends on remote services
            self._record_sentiment_error("历史行情失败 {}: {}".format(normalized, exc))
            return self._with_price_metadata(
                local if local is not None and not local.empty else cached,
                price_adjustment_mode="adjusted",
                data_source="cache_or_local",
            )
        if not fetched.empty:
            if self._write_history_cache(normalized, fetched):
                refreshed, _ = self._read_history_cache(normalized, days)
                if refreshed is not None and not refreshed.empty:
                    return self._with_price_metadata(refreshed, price_adjustment_mode="adjusted", data_source="cache")
            return self._with_price_metadata(
                fetched.tail(days).reset_index(drop=True),
                price_adjustment_mode="adjusted",
                data_source="remote_akshare",
            )
        return self._with_price_metadata(
            local if local is not None and not local.empty else cached,
            price_adjustment_mode="adjusted",
            data_source="cache_or_local",
        )

    def get_factor_bars_adjusted(self, code: str, days: int = 90) -> pd.DataFrame:
        """Get historical bars used for因子/收益校验（保留复权信息）。"""
        return self.get_history(code, days=days)

    def get_intraday_snapshot(self, as_of: str = "") -> pd.DataFrame:
        """Return a copy of realtime snapshot; `as_of` is for caller-side traceability."""
        quotes = self.get_realtime_quotes()
        frame = self._with_price_metadata(quotes, price_adjustment_mode="snapshot", data_source="realtime")
        if as_of:
            frame.attrs["snapshot_as_of"] = str(as_of)
        return frame

    def get_execution_bars_raw(self, code: str, days: int = 90) -> pd.DataFrame:
        """Return raw, unadjusted prices for fills and realized-return reconstruction."""
        normalized = normalize_code(code)
        try:
            local = load_execution_history_frames(
                config.MARKET_DATA_DB_PATH,
                [normalized],
                days=days,
            ).get(normalized, pd.DataFrame())
        except Exception as exc:
            self._record_sentiment_error("本地原始成交行情失败 {}: {}".format(normalized, exc))
            local = pd.DataFrame()
        if _usable_history(local, days):
            return self._with_price_metadata(local, price_adjustment_mode="raw", data_source="local_execution_db")
        try:
            fetched = self._fetch_akshare_execution_history(normalized, days)
        except Exception as exc:  # pragma: no cover - depends on remote services
            self._record_sentiment_error("原始成交行情失败 {}: {}".format(normalized, exc))
            fetched = pd.DataFrame()
        if fetched is not None and not fetched.empty:
            return self._with_price_metadata(fetched, price_adjustment_mode="raw", data_source="akshare_execution_fetch")
        fallback = self.get_history(normalized, days=days)
        if fallback is not None and not fallback.empty:
            fallback = self._with_price_metadata(fallback, price_adjustment_mode="fallback_adjusted", data_source="history_fallback")
            return fallback
        return self._with_price_metadata(local, price_adjustment_mode="raw", data_source="empty_fallback")

    def get_execution_history(self, code: str, days: int = 90) -> pd.DataFrame:
        """Backwards-compatible alias for execution bar access."""
        return self.get_execution_bars_raw(code, days=days)

    def _with_price_metadata(
        self,
        frame: Optional[pd.DataFrame],
        *,
        price_adjustment_mode: str,
        data_source: str,
    ) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame()
        payload = frame.copy(deep=True)
        attrs = dict(getattr(payload, "attrs", {}) or {})
        attrs["price_adjustment_mode"] = str(price_adjustment_mode)
        attrs["price_data_source"] = str(data_source)
        payload.attrs = attrs
        return payload

    def get_cached_history(self, code: str, days: int = 90) -> pd.DataFrame:
        normalized = normalize_code(code)
        cached, cache_fresh = self._read_history_cache(normalized, days)
        if _usable_history(cached, days) and cache_fresh:
            return self._with_price_metadata(cached, price_adjustment_mode="adjusted", data_source="history_cache")
        local = self._load_local_history(normalized, days)
        if _usable_history(local, days):
            return self._with_price_metadata(local, price_adjustment_mode="adjusted", data_source="local")
        return self._with_price_metadata(
            cached if cached is not None and not cached.empty else local,
            price_adjustment_mode="adjusted",
            data_source="cache_or_local",
        )

    def get_index_history(self, code: str = "000300", days: int = 90) -> pd.DataFrame:
        normalized = normalize_code(code)
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=max(days * 2, 120))).strftime("%Y%m%d")
        ak = self._get_akshare()
        try:
            frame = ak.index_zh_a_hist(
                symbol=normalized,
                period="daily",
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as first_error:
            try:
                frame = ak.stock_zh_index_daily_em(symbol="sh{}".format(normalized))
            except Exception as second_error:
                self._record_sentiment_error(
                    "指数历史失败 {}: {}; {}".format(normalized, first_error, second_error)
                )
                return pd.DataFrame()
        if frame is None or frame.empty:
            return pd.DataFrame()
        return rename_known_columns(frame).tail(days).reset_index(drop=True)

    def prefetch_history(self, codes: List[str], days: int = 180, force: bool = False) -> Dict[str, object]:
        result = {
            "requested": len(codes),
            "downloaded": 0,
            "cached": 0,
            "local": 0,
            "failed": 0,
            "errors": [],
        }
        seen = set()
        for code_value in codes:
            code = normalize_code(code_value)
            if not code or code in seen:
                continue
            seen.add(code)
            cached, cache_fresh = self._read_history_cache(code, days)
            if not force and _usable_history(cached, days) and cache_fresh:
                result["cached"] += 1
                continue
            local = self._load_local_history(code, days)
            if not force and _usable_history(local, days):
                result["cached"] += 1
                result["local"] += 1
                continue
            try:
                fetched = self._fetch_akshare_history(code, days)
                if fetched is None or fetched.empty:
                    raise RuntimeError("历史行情为空")
                if not self._write_history_cache(code, fetched):
                    raise RuntimeError("历史行情已下载但缓存写入失败")
                result["downloaded"] += 1
            except Exception as exc:  # pragma: no cover - depends on remote services
                if not force and local is not None and not local.empty:
                    result["cached"] += 1
                    result["local"] += 1
                    continue
                result["failed"] += 1
                result["errors"].append({"code": code, "error": str(exc)})
        result["unique_codes"] = len(seen)
        return result

    def _read_history_cache(self, code: str, days: int) -> Tuple[pd.DataFrame, bool]:
        try:
            cached = self._history_cache.get(code, days)
            return cached, self._history_cache.is_fresh(code)
        except Exception as exc:
            self._record_sentiment_error("历史缓存读取失败 {}: {}".format(code, exc))
            return pd.DataFrame(), False

    def _write_history_cache(self, code: str, history: pd.DataFrame) -> bool:
        try:
            self._history_cache.set(code, history)
            return True
        except Exception as exc:
            self._record_sentiment_error("历史缓存写入失败 {}: {}".format(code, exc))
            return False

    def _load_local_history(self, code: str, days: int) -> pd.DataFrame:
        try:
            return load_history_frames(config.MARKET_DATA_DB_PATH, [code], days=days).get(
                normalize_code(code),
                pd.DataFrame(),
            )
        except Exception as exc:
            self._record_sentiment_error("本地历史行情失败 {}: {}".format(code, exc))
            return pd.DataFrame()

    def _fetch_akshare_history(self, code: str, days: int) -> pd.DataFrame:
        ak = self._get_akshare()
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=max(days * 2, 120))).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(
            symbol=normalize_code(code),
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        return rename_known_columns(df).tail(days).reset_index(drop=True)

    def _fetch_akshare_execution_history(self, code: str, days: int) -> pd.DataFrame:
        ak = self._get_akshare()
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=max(days * 2, 120))).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(
            symbol=normalize_code(code),
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        output = rename_known_columns(df).tail(days).reset_index(drop=True)
        output.attrs["price_adjustment_mode"] = "raw"
        return output

    def health(self) -> Dict[str, object]:
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
        return {
            **status,
            "quote_background_refresh": self.quote_refresh_status(),
        }

    def _fetch_eastmoney_quotes(self) -> pd.DataFrame:
        df = _fetch_eastmoney_spot_dataframe()
        if df.empty:
            raise RuntimeError("东方财富直连返回空行情")
        return rename_known_columns(df)

    def _fetch_sina_quotes(self) -> pd.DataFrame:
        df = _fetch_sina_spot_dataframe()
        if df is None or df.empty:
            raise RuntimeError("新浪返回空行情")
        return rename_known_columns(df)

    def get_recommendation_quotes(self, codes) -> pd.DataFrame:
        start_time = time.perf_counter()
        try:
            raw = _fetch_tencent_recommendation_quotes(codes)
            quote_timestamp = str((raw.attrs or {}).get("quote_timestamp") or "")
            frame = rename_known_columns(raw)
            timestamp_by_code = {
                normalize_code(row.get("代码")): str(row.get("时间戳") or "")
                for row in raw.to_dict(orient="records")
            }
            frame["quote_timestamp"] = frame["code"].map(timestamp_by_code).fillna(quote_timestamp)
            frame["quote_source"] = "腾讯推荐池批量行情"
            frame.attrs["quote_timestamp"] = quote_timestamp
            frame.attrs["quote_source"] = "腾讯推荐池批量行情"
            requested = {normalize_code(code) for code in (codes or []) if normalize_code(code)}
            received = set(frame["code"].astype(str)) if "code" in frame.columns else set()
            frame.attrs["missing_codes"] = sorted(requested - received)
            frame.attrs["coverage_ratio"] = round(len(received & requested) / max(1, len(requested)), 4)
            self._record_quote_fetch_result(
                source="腾讯推荐池批量行情",
                success=True,
                latency_ms=max(0.0, (time.perf_counter() - start_time) * 1000.0),
            )
            return frame
        except Exception as exc:
            self._record_quote_fetch_result(
                source="腾讯推荐池批量行情",
                success=False,
                latency_ms=max(0.0, (time.perf_counter() - start_time) * 1000.0),
                error=str(exc),
            )
            raise

    def _fetch_tushare_quotes(self) -> pd.DataFrame:
        token = config.TUSHARE_TOKEN
        if not token:
            raise RuntimeError("未配置 TUSHARE_TOKEN")
        ts = self._get_tushare()
        pro = self._get_tushare_api(token)
        try:
            df = ts.realtime_quote(ts_code="")
        except Exception:
            trade_date = datetime.now().strftime("%Y%m%d")
            df = pro.daily(trade_date=trade_date)
        if df is None or df.empty:
            raise RuntimeError("Tushare 返回空行情")
        return rename_known_columns(df)

    def _get_akshare(self):
        if self._akshare is None:
            import akshare as ak

            self._akshare = ak
        return self._akshare

    def _get_tushare(self):
        if self._tushare is None:
            import tushare as ts

            self._tushare = ts
        return self._tushare

    def _get_tushare_api(self, token: str):
        if self._tushare_api is None:
            ts = self._get_tushare()
            ts.set_token(token)
            self._tushare_api = ts.pro_api(token)
        return self._tushare_api

    def _record_sentiment_error(self, message: str) -> None:
        lock = getattr(self, "_status_lock", None)
        if lock is None:
            errors = list(getattr(self.status, "errors", []) or [])
            errors.append(message)
            self.status.errors = errors[-20:]
            return
        with lock:
            self.status.errors.append(message)
            self.status.errors = self.status.errors[-20:]

    def _save_quote_snapshot(self, df: pd.DataFrame) -> None:
        if df is None or df.empty or len(df) < config.QUOTE_SNAPSHOT_MIN_ROWS:
            return
        path = Path(config.QUOTE_SNAPSHOT_PATH)
        try:
            snapshot = df.copy()
            quote_timestamp = str((df.attrs or {}).get("quote_timestamp") or "").strip()
            if quote_timestamp:
                snapshot["__quote_timestamp"] = quote_timestamp
            atomic_write_text(path, snapshot.to_json(orient="records", force_ascii=False))
        except Exception:
            return

    def _load_quote_snapshot(self) -> Optional[pd.DataFrame]:
        path = Path(config.QUOTE_SNAPSHOT_PATH)
        try:
            if not path.exists():
                return None
            stat = path.stat()
            age = time.time() - stat.st_mtime
            max_age = int(getattr(config, "QUOTE_SNAPSHOT_MAX_AGE_SECONDS", 21600))
            now = datetime.now()
            clock = now.strftime("%H:%M")
            if now.weekday() < 5 and ("09:15" <= clock <= "11:35" or "13:00" <= clock <= "15:10"):
                max_age = min(
                    max_age,
                    max(30, int(getattr(config, "QUOTE_SNAPSHOT_INTRADAY_MAX_AGE_SECONDS", 90))),
                )
            if age > max_age:
                return None
            df = pd.read_json(path)
        except Exception:
            return None
        if df.empty or len(df) < config.QUOTE_SNAPSHOT_MIN_ROWS:
            return None
        if "__quote_timestamp" in df.columns:
            timestamps = [str(value).strip() for value in df["__quote_timestamp"].dropna().tolist() if str(value).strip()]
            if timestamps:
                df.attrs["quote_timestamp"] = timestamps[0]
            df = df.drop(columns=["__quote_timestamp"])
        df.attrs["snapshot_mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        return df


class TimedCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._value = None
        self._expires_at = 0.0
        self._stats = {
            "hits": 0,
            "misses": 0,
            "expired": 0,
            "sets": 0,
            "clears": 0,
            "set_bytes": 0,
            "hit_bytes": 0,
        }

    @staticmethod
    def _copy_value(value):
        if isinstance(value, pd.DataFrame):
            return value.copy(deep=True)
        if isinstance(value, pd.Series):
            return value.copy(deep=True)
        if isinstance(value, dict):
            return copy.deepcopy(value)
        if isinstance(value, (list, tuple, set)):
            return copy.deepcopy(value)
        return copy.deepcopy(value)

    @staticmethod
    def _estimate_bytes(value) -> int:
        try:
            return len(str(value))
        except Exception:
            return 0

    def get(self):
        now = time.time()
        with self._lock:
            if self._value is None or now >= self._expires_at:
                if self._value is not None and now >= self._expires_at:
                    self._stats["expired"] += 1
                self._value = None
                self._stats["misses"] += 1
                self._stats["hit_bytes"] = 0
                return None
            self._stats["hits"] += 1
            cached = self._copy_value(self._value)
            self._stats["hit_bytes"] = self._estimate_bytes(cached)
            return cached

    def set(self, value):
        cloned = self._copy_value(value)
        with self._lock:
            self._value = cloned
            self._expires_at = time.time() + self.ttl_seconds
            self._stats["sets"] += 1
            self._stats["set_bytes"] = self._estimate_bytes(cloned)
            self._stats["hit_bytes"] = self._stats["set_bytes"]

    def clear(self):
        with self._lock:
            self._value = None
            self._expires_at = 0.0
            self._stats["clears"] += 1
            self._stats["hit_bytes"] = 0

    def stats(self) -> Dict[str, object]:
        with self._lock:
            now = time.time()
            expired = self._value is not None and now >= self._expires_at
            if expired:
                return {
                    **self._stats,
                    "entries": 0,
                    "expired": self._stats["expired"] + 1,
                    "memory_bytes": self._stats["set_bytes"],
                    "ttl_seconds": self.ttl_seconds,
                }
            return {
                **self._stats,
                "entries": 0 if self._value is None else 1,
                "memory_bytes": self._stats["set_bytes"],
                "ttl_seconds": self.ttl_seconds,
            }


EASTMONEY_FIELDS = (
    "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f24,f25,f22,f11,f62,f100,f124,f128,f136,f115,f152"
)
EASTMONEY_COLUMN_MAP = {
    "f2": "最新价",
    "f3": "涨跌幅",
    "f4": "涨跌额",
    "f5": "成交量",
    "f6": "成交额",
    "f7": "振幅",
    "f8": "换手率",
    "f9": "市盈率-动态",
    "f10": "量比",
    "f11": "5分钟涨跌",
    "f12": "代码",
    "f14": "名称",
    "f15": "最高",
    "f16": "最低",
    "f17": "今开",
    "f18": "昨收",
    "f20": "总市值",
    "f21": "流通市值",
    "f22": "涨速",
    "f23": "市净率",
    "f24": "60日涨跌幅",
    "f25": "年初至今涨跌幅",
    "f100": "行业",
    "f124": "行情时间戳",
}
EASTMONEY_NUMERIC_COLUMNS = (
    "最新价",
    "涨跌幅",
    "涨跌额",
    "成交量",
    "成交额",
    "振幅",
    "换手率",
    "市盈率-动态",
    "量比",
    "5分钟涨跌",
    "最高",
    "最低",
    "今开",
    "昨收",
    "总市值",
    "流通市值",
    "涨速",
    "市净率",
    "60日涨跌幅",
    "年初至今涨跌幅",
)


def _fetch_eastmoney_spot_dataframe() -> pd.DataFrame:
    params = _eastmoney_spot_params(page=1)
    first_json = _request_eastmoney_page(params)
    data = first_json.get("data") or {}
    rows = data.get("diff") or []
    if not rows:
        return pd.DataFrame()

    page_size = max(len(rows), 1)
    total = int(data.get("total") or len(rows))
    total_pages = max(1, (total + page_size - 1) // page_size)
    if config.EASTMONEY_MAX_PAGES > 0:
        total_pages = min(total_pages, config.EASTMONEY_MAX_PAGES)

    def fetch_page(page: int):
        page_json = _request_eastmoney_page(_eastmoney_spot_params(page=page))
        return (page_json.get("data") or {}).get("diff") or []

    page_rows = _download_quote_pages(
        range(2, total_pages + 1),
        fetch_page,
        source="东方财富",
        max_workers=config.EASTMONEY_CONCURRENCY,
        retries=config.EASTMONEY_PAGE_RETRIES,
        batch_timeout_seconds=config.EASTMONEY_BATCH_TIMEOUT_SECONDS,
    )
    frames = [pd.DataFrame(rows)]
    frames.extend(pd.DataFrame(page_rows[page]) for page in sorted(page_rows))
    raw = pd.concat(frames, ignore_index=True)
    expected_rows = min(total, total_pages * page_size)
    raw = _validate_quote_coverage(raw, "f12", expected_rows, "东方财富")
    return _normalize_eastmoney_spot(raw)


def _eastmoney_spot_params(page: int) -> Dict[str, str]:
    return {
        "pn": str(page),
        "pz": str(config.EASTMONEY_PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": config.EASTMONEY_SORT_FIELD,
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
        "fields": EASTMONEY_FIELDS,
    }


def _request_eastmoney_page(params: Dict[str, str]) -> Dict[str, object]:
    last_error: Optional[Exception] = None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/center/gridlist.html",
    }
    for host in ("82.push2.eastmoney.com", "push2.eastmoney.com", "7.push2.eastmoney.com"):
        url = "https://{}/api/qt/clist/get".format(host)
        for trust_env in (True, False):
            try:
                with requests.Session() as session:
                    session.trust_env = trust_env
                    response = session.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=config.EASTMONEY_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    payload = response.json()
            except Exception as exc:
                last_error = exc
                continue
            if payload.get("data"):
                return payload
            last_error = RuntimeError("东方财富返回空 data")
    raise RuntimeError(str(last_error) if last_error else "东方财富请求失败")


SINA_QUOTE_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
SINA_QUOTE_COUNT_URL = (
    "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
)
SINA_NUMERIC_COLUMN_MAP = {
    "trade": "最新价",
    "pricechange": "涨跌额",
    "changepercent": "涨跌幅",
    "buy": "买入",
    "sell": "卖出",
    "settlement": "昨收",
    "open": "今开",
    "high": "最高",
    "low": "最低",
    "volume": "成交量",
    "amount": "成交额",
}


def _fetch_tencent_recommendation_quotes(codes) -> pd.DataFrame:
    normalized_codes = sorted(
        {
            normalize_code(code)
            for code in (codes or [])
            if normalize_code(code) and len(normalize_code(code)) == 6
        }
    )
    if not normalized_codes:
        return pd.DataFrame()

    def symbol(code: str) -> str:
        if code.startswith(("6", "9")):
            return "sh" + code
        if code.startswith(("4", "8")):
            return "bj" + code
        return "sz" + code

    last_error: Optional[Exception] = None
    content = b""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": "https://gu.qq.com/",
    }
    for trust_env in (True, False):
        try:
            with requests.Session() as session:
                session.trust_env = trust_env
                response = session.get(
                    "https://qt.gtimg.cn/q={}".format(",".join(symbol(code) for code in normalized_codes)),
                    headers=headers,
                    timeout=max(1.0, float(getattr(config, "RECOMMENDATION_QUOTE_TIMEOUT_SECONDS", 4))),
                )
                response.raise_for_status()
                content = response.content
        except Exception as exc:
            last_error = exc
            continue
        if content.strip():
            break
    if not content.strip():
        raise RuntimeError(str(last_error) if last_error else "腾讯推荐池行情返回空响应")

    rows = []
    timestamps = []
    text = content.decode("gb18030", errors="replace")
    for payload in re.findall(r'v_[^=]+="([^"]*)";', text):
        fields = payload.split("~")
        if len(fields) < 50:
            continue
        code = normalize_code(fields[2])
        if code not in normalized_codes:
            continue

        def number(index: int, default=None):
            try:
                value = fields[index].strip()
                return float(value) if value else default
            except (IndexError, TypeError, ValueError):
                return default

        timestamp = ""
        raw_timestamp = fields[30].strip()
        try:
            timestamp = datetime.strptime(raw_timestamp, "%Y%m%d%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            timestamp = ""
        if timestamp:
            timestamps.append(timestamp)
        transaction = fields[35].split("/") if len(fields) > 35 else []
        volume = number(36)
        if code.startswith(("0", "3")) and volume is not None:
            volume *= 100.0
        amount = None
        if len(transaction) >= 3:
            try:
                amount = float(transaction[2])
            except (TypeError, ValueError):
                amount = None
        rows.append(
            {
                "代码": code,
                "名称": fields[1].strip(),
                "最新价": number(3),
                "涨跌幅": number(32),
                "今开": number(5),
                "最高": number(33),
                "最低": number(34),
                "成交量": volume,
                "成交额": amount,
                "换手率": number(38),
                "振幅": number(43),
                "量比": number(49),
                "时间戳": timestamp,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("腾讯推荐池行情没有可用股票记录")
    frame.attrs["quote_timestamp"] = max(timestamps) if timestamps else datetime.now().isoformat(timespec="seconds")
    return frame


def _fetch_sina_spot_dataframe() -> pd.DataFrame:
    count_text = _request_sina_text(SINA_QUOTE_COUNT_URL, {"node": "hs_a"})
    count_match = re.search(r"\d+", count_text)
    if count_match is None:
        raise RuntimeError("新浪行情总数格式异常")
    total = int(count_match.group(0))
    page_size = max(1, int(config.SINA_QUOTE_PAGE_SIZE))
    total_pages = max(1, math.ceil(total / page_size))

    def fetch_page(page: int):
        text = _request_sina_text(
            SINA_QUOTE_URL,
            {
                "page": str(page),
                "num": str(page_size),
                "sort": "symbol",
                "asc": "1",
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            },
        )
        return _decode_sina_rows(text)

    page_rows = _download_quote_pages(
        range(1, total_pages + 1),
        fetch_page,
        source="新浪",
        max_workers=config.SINA_QUOTE_CONCURRENCY,
        retries=config.SINA_QUOTE_PAGE_RETRIES,
        batch_timeout_seconds=config.SINA_QUOTE_BATCH_TIMEOUT_SECONDS,
    )
    raw = pd.DataFrame([row for page in sorted(page_rows) for row in page_rows[page]])
    raw = _validate_quote_coverage(raw, "code", total, "新浪", fallback_code_column="symbol")
    return _normalize_sina_spot(raw)


def _request_sina_text(url: str, params: Dict[str, str]) -> str:
    last_error: Optional[Exception] = None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://vip.stock.finance.sina.com.cn/mkt/",
    }
    for trust_env in (False, True):
        try:
            with requests.Session() as session:
                session.trust_env = trust_env
                response = session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=config.SINA_QUOTE_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                text = response.text
        except Exception as exc:
            last_error = exc
            continue
        if text.strip():
            return text
        last_error = RuntimeError("新浪返回空响应")
    raise RuntimeError(str(last_error) if last_error else "新浪请求失败")


def _decode_sina_rows(text: str) -> List[Dict[str, object]]:
    from akshare.utils import demjson

    payload = demjson.decode(text)
    if not isinstance(payload, list):
        raise RuntimeError("新浪行情分页格式异常")
    return [row for row in payload if isinstance(row, dict)]


def _normalize_sina_spot(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    codes = raw.get("code", pd.Series(index=raw.index, dtype=object))
    if "symbol" in raw.columns:
        codes = codes.where(codes.notna() & codes.astype(str).str.strip().ne(""), raw["symbol"])
    result = pd.DataFrame(
        {
            "代码": codes.map(normalize_code),
            "名称": raw.get("name", pd.Series(index=raw.index, dtype=object)).fillna("").astype(str),
            "时间戳": raw.get("ticktime", pd.Series(index=raw.index, dtype=object)).fillna("").astype(str),
        }
    )
    for source_column, target_column in SINA_NUMERIC_COLUMN_MAP.items():
        values = raw.get(source_column, pd.Series(index=raw.index, dtype=float))
        result[target_column] = pd.to_numeric(values, errors="coerce")
    result = result[result["代码"].astype(str).str.len() == 6]
    return result.sort_values("代码", kind="stable").reset_index(drop=True)


def _download_quote_pages(
    pages,
    fetch_page,
    source: str,
    max_workers: int,
    retries: int,
    batch_timeout_seconds: float,
) -> Dict[int, List[Dict[str, object]]]:
    page_numbers = list(pages)
    if not page_numbers:
        return {}
    deadline = time.monotonic() + max(1.0, float(batch_timeout_seconds))

    def run(page: int):
        last_error: Optional[Exception] = None
        for attempt in range(max(0, int(retries)) + 1):
            if time.monotonic() >= deadline:
                raise RuntimeError("超过批次截止时间")
            try:
                rows = fetch_page(page)
                if not rows:
                    raise RuntimeError("返回空分页")
                return rows
            except Exception as exc:
                last_error = exc
                if attempt < max(0, int(retries)):
                    time.sleep(min(0.2 * (attempt + 1), 0.5))
        raise RuntimeError(str(last_error) if last_error else "分页请求失败")

    results: Dict[int, List[Dict[str, object]]] = {}
    errors: Dict[int, str] = {}
    configured_limit = max(1, int(getattr(config, "QUOTE_PAGE_WORKER_LIMIT", int(max_workers) or 1)))
    worker_count = min(len(page_numbers), max(1, int(max_workers), 1))
    worker_count = min(worker_count, configured_limit)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="quote-pages") as executor:
        futures = {executor.submit(run, page): page for page in page_numbers}
        for future in as_completed(futures):
            page = futures[future]
            try:
                results[page] = future.result()
            except Exception as exc:
                errors[page] = str(exc)

    # Public quote endpoints occasionally throttle the tail of a concurrent batch.
    # Repair only the missing pages serially with a fresh, bounded deadline.  The
    # caller still validates full-universe code coverage afterwards, so this does
    # not turn an incomplete download into an accepted snapshot.
    if errors:
        repair_deadline = time.monotonic() + max(5.0, min(30.0, float(batch_timeout_seconds)))
        repair_attempts = max(2, int(retries) + 1)
        for page in sorted(list(errors)):
            last_error: Optional[Exception] = None
            for attempt in range(repair_attempts):
                if time.monotonic() >= repair_deadline:
                    last_error = RuntimeError("缺页修复超过截止时间")
                    break
                if attempt:
                    time.sleep(min(0.5 * attempt, 1.0))
                try:
                    rows = fetch_page(page)
                    if not rows:
                        raise RuntimeError("返回空分页")
                    results[page] = rows
                    errors.pop(page, None)
                    break
                except Exception as exc:
                    last_error = exc
            if page in errors and last_error is not None:
                errors[page] = str(last_error)
    if errors:
        details = ", ".join("{}={}".format(page, errors[page]) for page in sorted(errors)[:5])
        raise RuntimeError("{}分页下载失败: {}".format(source, details))
    return results


def _validate_quote_coverage(
    raw: pd.DataFrame,
    code_column: str,
    expected_rows: int,
    source: str,
    fallback_code_column: str = "",
) -> pd.DataFrame:
    if raw.empty:
        raise RuntimeError("{}行情为空".format(source))
    codes = raw.get(code_column, pd.Series(index=raw.index, dtype=object))
    if fallback_code_column and fallback_code_column in raw.columns:
        codes = codes.where(codes.notna() & codes.astype(str).str.strip().ne(""), raw[fallback_code_column])
    normalized_codes = codes.map(normalize_code)
    valid = normalized_codes.astype(str).str.fullmatch(r"\d{6}")
    result = raw.loc[valid].copy()
    result[code_column] = normalized_codes.loc[valid]
    result = result.drop_duplicates(subset=[code_column], keep="first")
    minimum_ratio = min(1.0, max(0.5, float(config.QUOTE_DOWNLOAD_MIN_COVERAGE_RATIO)))
    minimum_rows = math.ceil(max(1, int(expected_rows)) * minimum_ratio)
    if len(result) < minimum_rows:
        raise RuntimeError(
            "{}行情覆盖不足: 唯一代码 {} / 预期 {}, 最低比例 {:.0%}".format(
                source,
                len(result),
                expected_rows,
                minimum_ratio,
            )
        )
    return result.sort_values(code_column, kind="stable").reset_index(drop=True)


def _normalize_eastmoney_spot(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    df = raw.rename(columns=EASTMONEY_COLUMN_MAP)
    required_columns = ["代码", "名称", "最新价", "涨跌幅", "成交额"]
    missing = [column for column in required_columns if column not in df.columns]
    if missing:
        raise RuntimeError("东方财富行情缺少字段: {}".format(",".join(missing)))

    columns = [
        "代码",
        "名称",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "成交量",
        "成交额",
        "振幅",
        "最高",
        "最低",
        "今开",
        "昨收",
        "量比",
        "换手率",
        "市盈率-动态",
        "市净率",
        "总市值",
        "流通市值",
        "涨速",
        "5分钟涨跌",
        "60日涨跌幅",
        "年初至今涨跌幅",
    ]
    for column in columns:
        if column not in df.columns:
            df[column] = 0.0
    for column in EASTMONEY_NUMERIC_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    result = df[columns].reset_index(drop=True)
    timestamp_source = df["行情时间戳"] if "行情时间戳" in df.columns else pd.Series(dtype=float)
    timestamps = pd.to_numeric(timestamp_source, errors="coerce").dropna()
    if not timestamps.empty and float(timestamps.max()) > 0:
        result.attrs["quote_timestamp"] = datetime.fromtimestamp(float(timestamps.max())).isoformat(timespec="seconds")
    return result


def _extract_news_rows(
    df: pd.DataFrame,
    source: str,
    limit: int,
    keyword: str = "",
) -> List[Dict[str, str]]:
    if df is None or df.empty:
        return []
    items: List[Dict[str, str]] = []
    keyword = keyword.strip()
    for _, row in df.head(limit * 4).iterrows():
        title = _first_present(row, ("新闻标题", "标题", "title", "内容", "摘要"))
        content = _first_present(row, ("新闻内容", "内容", "摘要", "summary"))
        if keyword and keyword not in title and keyword not in content:
            continue
        publish_time = _first_present(row, ("发布时间", "时间", "datetime", "date", "日期"))
        url = _first_present(row, ("新闻链接", "链接", "url"))
        if not title and content:
            title = content[:80]
        if not title:
            continue
        items.append(
            {
                "source": source,
                "title": title,
                "content": content,
                "publish_time": publish_time,
                "url": url,
            }
        )
        if len(items) >= limit:
            break
    return items


def _event_rows(
    df: pd.DataFrame,
    code_keys: Tuple[str, ...],
    date_keys: Tuple[str, ...] = (),
    ratio_keys: Tuple[str, ...] = (),
) -> List[Dict[str, object]]:
    if df is None or df.empty:
        return []
    rows: List[Dict[str, object]] = []
    for _, row in df.iterrows():
        code = _first_raw(row, code_keys)
        if code in (None, ""):
            continue
        item: Dict[str, object] = {"code": normalize_code(code)}
        date = _first_raw(row, date_keys)
        ratio = _first_raw(row, ratio_keys)
        if date not in (None, ""):
            item["date"] = date
        if ratio not in (None, ""):
            item["unlock_ratio"] = ratio
            item["pledge_ratio"] = ratio
        rows.append(item)
    return rows


def _first_raw(row: pd.Series, columns: Tuple[str, ...]):
    for column in columns:
        if column in row and pd.notna(row[column]):
            value = row[column]
            if str(value).strip() not in ("", "-", "--", "nan"):
                return value
    return None


def _merge_fundamental_item(items: Dict[str, Dict[str, object]], code: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    row = df.iloc[0]
    item = items.setdefault(code, {"code": code})
    mappings = {
        "roe": ("roe", "ROE", "净资产收益率", "净资产收益率(%)", "roe_dt"),
        "gross_margin": ("gross_margin", "销售毛利率", "毛利率", "grossprofit_margin"),
        "debt_ratio": ("debt_ratio", "资产负债率", "资产负债率(%)", "debt_to_assets"),
        "earnings_surprise": ("earnings_surprise", "业绩变动幅度", "净利润增长率", "q_profit_yoy", "or_yoy"),
        "rating_revision": ("rating_revision", "rating_revision", "预测调整", "盈利预测调整"),
    }
    for target, columns in mappings.items():
        value = _first_raw(row, columns)
        if value not in (None, ""):
            item[target] = _to_float(value)


def _usable_history(history: pd.DataFrame, days: int) -> bool:
    return history is not None and not history.empty and len(history) >= min(days, 30)


def _to_ts_code(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("600", "601", "603", "605", "688")):
        return "{}.SH".format(code)
    return "{}.SZ".format(code)


def _first_present(row: pd.Series, columns: Tuple[str, ...]) -> str:
    for column in columns:
        if column in row and pd.notna(row[column]):
            value = str(row[column]).strip()
            if value and value not in ("-", "--", "nan"):
                return value
    return ""


def _to_float(value) -> float:
    try:
        return float(str(value).replace("%", "").replace(",", ""))
    except Exception:
        return 0.0
