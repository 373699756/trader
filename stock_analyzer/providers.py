import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

from . import config
from .history_cache import HistoryCache
from .normalization import normalize_code, rename_known_columns


@dataclass
class ProviderStatus:
    quotes_source: str = "unavailable"
    sentiment_source: str = "unavailable"
    last_quote_refresh: Optional[str] = None
    last_sentiment_refresh: Optional[str] = None
    errors: List[str] = field(default_factory=list)


class MarketDataProvider:
    def __init__(self) -> None:
        self.status = ProviderStatus()
        self._akshare = None
        self._tushare = None
        self._tushare_api = None
        self._history_cache = HistoryCache(
            config.HISTORY_CACHE_PATH,
            freshness_hours=config.HISTORY_CACHE_FRESHNESS_HOURS,
        )

    def get_realtime_quotes(self) -> pd.DataFrame:
        errors = []
        try:
            df = self._fetch_eastmoney_quotes()
            self.status.quotes_source = "东方财富直连"
            self.status.last_quote_refresh = datetime.now().isoformat(timespec="seconds")
            self.status.errors = []
            self._save_quote_snapshot(df)
            return df
        except Exception as exc:  # pragma: no cover - depends on remote services
            errors.append("东方财富直连行情失败: {}".format(exc))

        if not config.ALLOW_SLOW_QUOTE_FALLBACK:
            snapshot = self._load_quote_snapshot()
            if snapshot is not None and not snapshot.empty:
                self.status.quotes_source = "本地快照"
                self.status.last_quote_refresh = datetime.now().isoformat(timespec="seconds")
                self.status.errors = errors
                return snapshot
            self.status.quotes_source = "unavailable"
            self.status.errors = errors
            raise RuntimeError("; ".join(errors))

        try:
            df = self._fetch_akshare_quotes()
            self.status.quotes_source = "AKShare 东方财富"
            self.status.last_quote_refresh = datetime.now().isoformat(timespec="seconds")
            self.status.errors = errors
            self._save_quote_snapshot(df)
            return df
        except Exception as exc:  # pragma: no cover - depends on remote services
            errors.append("AKShare 行情失败: {}".format(exc))

        try:
            df = self._fetch_sina_quotes()
            self.status.quotes_source = "AKShare 新浪"
            self.status.last_quote_refresh = datetime.now().isoformat(timespec="seconds")
            self.status.errors = errors
            self._save_quote_snapshot(df)
            return df
        except Exception as exc:  # pragma: no cover - depends on remote services
            errors.append("新浪行情失败: {}".format(exc))

        try:
            df = self._fetch_tushare_quotes()
            self.status.quotes_source = "Tushare"
            self.status.last_quote_refresh = datetime.now().isoformat(timespec="seconds")
            self.status.errors = errors
            self._save_quote_snapshot(df)
            return df
        except Exception as exc:  # pragma: no cover - depends on remote services
            errors.append("Tushare 行情失败: {}".format(exc))

        self.status.quotes_source = "unavailable"
        self.status.errors = errors
        raise RuntimeError("; ".join(errors))

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
        cached = self._history_cache.get(code, days)
        if not cached.empty and len(cached) >= min(days, 30) and self._history_cache.is_fresh(code):
            return cached
        try:
            fetched = self._fetch_akshare_history(code, days)
        except Exception as exc:  # pragma: no cover - depends on remote services
            self._record_sentiment_error("历史行情失败 {}: {}".format(code, exc))
            return cached
        if not fetched.empty:
            self._history_cache.set(code, fetched)
            cached = self._history_cache.get(code, days)
            if not cached.empty:
                return cached
        return cached

    def get_cached_history(self, code: str, days: int = 90) -> pd.DataFrame:
        return self._history_cache.get(code, days)

    def prefetch_history(self, codes: List[str], days: int = 180, force: bool = False) -> Dict[str, object]:
        result = {
            "requested": len(codes),
            "downloaded": 0,
            "cached": 0,
            "failed": 0,
            "errors": [],
        }
        seen = set()
        for code_value in codes:
            code = normalize_code(code_value)
            if not code or code in seen:
                continue
            seen.add(code)
            cached = self._history_cache.get(code, days)
            if not force and not cached.empty and len(cached) >= min(days, 30) and self._history_cache.is_fresh(code):
                result["cached"] += 1
                continue
            try:
                fetched = self._fetch_akshare_history(code, days)
                if fetched is None or fetched.empty:
                    raise RuntimeError("历史行情为空")
                self._history_cache.set(code, fetched)
                result["downloaded"] += 1
            except Exception as exc:  # pragma: no cover - depends on remote services
                result["failed"] += 1
                result["errors"].append({"code": code, "error": str(exc)})
        result["unique_codes"] = len(seen)
        return result

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

    def health(self) -> Dict[str, object]:
        return {
            "quotes_source": self.status.quotes_source,
            "sentiment_source": self.status.sentiment_source,
            "last_quote_refresh": self.status.last_quote_refresh,
            "last_sentiment_refresh": self.status.last_sentiment_refresh,
            "errors": self.status.errors[-10:],
        }

    def _fetch_akshare_quotes(self) -> pd.DataFrame:
        ak = self._get_akshare()
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            raise RuntimeError("AKShare 返回空行情")
        return rename_known_columns(df)

    def _fetch_eastmoney_quotes(self) -> pd.DataFrame:
        df = _fetch_eastmoney_spot_dataframe()
        if df.empty:
            raise RuntimeError("东方财富直连返回空行情")
        return rename_known_columns(df)

    def _fetch_sina_quotes(self) -> pd.DataFrame:
        ak = self._get_akshare()
        df = ak.stock_zh_a_spot()
        if df is None or df.empty:
            raise RuntimeError("新浪返回空行情")
        return rename_known_columns(df)

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
        self.status.errors.append(message)
        self.status.errors = self.status.errors[-20:]

    def _save_quote_snapshot(self, df: pd.DataFrame) -> None:
        if df is None or df.empty or len(df) < config.QUOTE_SNAPSHOT_MIN_ROWS:
            return
        path = Path(config.QUOTE_SNAPSHOT_PATH)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_json(path, orient="records", force_ascii=False)
        except Exception:
            return

    def _load_quote_snapshot(self) -> Optional[pd.DataFrame]:
        path = Path(config.QUOTE_SNAPSHOT_PATH)
        try:
            if not path.exists():
                return None
            age = time.time() - path.stat().st_mtime
            if age > config.QUOTE_SNAPSHOT_MAX_AGE_SECONDS:
                return None
            df = pd.read_json(path)
        except Exception:
            return None
        if df.empty or len(df) < config.QUOTE_SNAPSHOT_MIN_ROWS:
            return None
        return df


class TimedCache:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._value = None
        self._expires_at = 0.0

    def get(self):
        if time.time() < self._expires_at:
            return self._value
        return None

    def set(self, value):
        self._value = value
        self._expires_at = time.time() + self.ttl_seconds

    def clear(self):
        self._value = None
        self._expires_at = 0.0


EASTMONEY_FIELDS = (
    "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f24,f25,f22,f11,f62,f100,f128,f136,f115,f152"
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
    frames = []
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
    frames.append(pd.DataFrame(rows))

    for page in range(2, total_pages + 1):
        params = _eastmoney_spot_params(page=page)
        try:
            page_json = _request_eastmoney_page(params)
        except Exception:
            break
        page_rows = (page_json.get("data") or {}).get("diff") or []
        if not page_rows:
            continue
        frames.append(pd.DataFrame(page_rows))

    raw = pd.concat(frames, ignore_index=True)
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
    for host in ("push2.eastmoney.com", "82.push2.eastmoney.com", "7.push2.eastmoney.com"):
        for scheme in ("https", "http"):
            url = "{}://{}/api/qt/clist/get".format(scheme, host)
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
    return df[columns].reset_index(drop=True)


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
