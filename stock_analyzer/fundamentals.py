import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Sequence, Tuple

import pandas as pd

from . import config
from .normalization import coerce_number, finite_series, normalize_code, percentile_score
from .runtime_json import atomic_write_json


FUNDAMENTAL_COLUMNS = (
    "roe",
    "gross_margin",
    "debt_ratio",
    "pe_dynamic",
    "pb",
    "earnings_surprise",
    "rating_revision",
)

FUNDAMENTAL_RESEARCH_COLUMNS = (
    "revenue_yoy",
    "net_profit_yoy",
    "operating_cashflow",
    "operating_cashflow_yoy",
    "free_cashflow",
    "current_ratio",
    "receivables_yoy",
    "inventory_yoy",
    "goodwill_ratio",
    "interest_bearing_debt_ratio",
)

ALL_FUNDAMENTAL_COLUMNS = FUNDAMENTAL_COLUMNS + FUNDAMENTAL_RESEARCH_COLUMNS

FUNDAMENTAL_META_COLUMNS = (
    "announcement_time",
    "report_period",
    "source_timestamp",
)


@dataclass
class FundamentalFetchResult:
    source: str
    status: str = "empty"
    items: Dict[str, Dict[str, object]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, object]:
        return {
            "source": self.source,
            "status": self.status,
            "items": self.items,
            "errors": list(self.errors),
            "sources": list(self.sources or ([self.source] if self.source else [])),
        }


@dataclass
class HistoryLookupResult:
    code: str
    days: int
    source: str
    status: str
    history: pd.DataFrame = field(default_factory=pd.DataFrame)
    cache_fresh: bool = False
    fallback_used: bool = False
    error: str = ""

    @property
    def usable(self) -> bool:
        return _usable_history_frame(self.history, self.days)

    def to_payload(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "days": self.days,
            "source": self.source,
            "status": self.status,
            "row_count": 0 if self.history is None else int(len(self.history)),
            "cache_fresh": bool(self.cache_fresh),
            "fallback_used": bool(self.fallback_used),
            "usable": bool(self.usable),
            "error": self.error,
        }


class FundamentalProviderAdapter:
    source = "provider"

    def __init__(self, provider) -> None:
        self.provider = provider

    def available(self) -> bool:
        return self.provider is not None

    def fetch(self, codes: Sequence[str]) -> FundamentalFetchResult:
        raise NotImplementedError

    def _error(self, message: str) -> None:
        recorder = getattr(self.provider, "_record_sentiment_error", None)
        if callable(recorder):
            recorder(message)


class EastmoneyFundamentalAdapter(FundamentalProviderAdapter):
    source = "eastmoney"

    def available(self) -> bool:
        return self.provider is not None and (
            callable(getattr(self.provider, "get_eastmoney_fundamental_factors", None))
            or callable(getattr(self.provider, "fetch_eastmoney_fundamentals", None))
        )

    def fetch(self, codes: Sequence[str]) -> FundamentalFetchResult:
        try:
            fetcher = getattr(self.provider, "get_eastmoney_fundamental_factors", None) or getattr(
                self.provider,
                "fetch_eastmoney_fundamentals",
                None,
            )
            try:
                raw_items = fetcher(codes=list(codes))
            except TypeError:
                raw_items = fetcher(list(codes))
            items = _normalize_fundamental_items(raw_items)
            return FundamentalFetchResult(self.source, "ok" if items else "empty", items, sources=[self.source])
        except Exception as exc:
            message = "Eastmoney 财务指标失败: {}".format(exc)
            self._error(message)
            return FundamentalFetchResult(self.source, "error", {}, [message], sources=[self.source])


class TushareFundamentalAdapter(FundamentalProviderAdapter):
    source = "tushare"

    def available(self) -> bool:
        return bool(getattr(config, "TUSHARE_TOKEN", "")) and callable(getattr(self.provider, "_get_tushare_api", None))

    def fetch(self, codes: Sequence[str]) -> FundamentalFetchResult:
        items: Dict[str, Dict[str, object]] = {}
        errors = []
        try:
            pro = self.provider._get_tushare_api(config.TUSHARE_TOKEN)
        except Exception as exc:
            message = "Tushare 财务指标失败: {}".format(exc)
            self._error(message)
            return FundamentalFetchResult(self.source, "error", {}, [message], sources=[self.source])
        for code in codes:
            try:
                frame = pro.fina_indicator(ts_code=_to_ts_code(code), limit=1)
                _merge_fundamental_frame(items, code, frame)
            except Exception as exc:
                errors.append("{}: {}".format(code, exc))
        if errors:
            self._error("Tushare 财务指标部分失败: {}".format("; ".join(errors[:5])))
        return FundamentalFetchResult(
            self.source,
            "ok" if items else "empty",
            items,
            errors,
            sources=[self.source],
        )


class AKShareFundamentalAdapter(FundamentalProviderAdapter):
    source = "akshare"

    def available(self) -> bool:
        return callable(getattr(self.provider, "_get_akshare", None))

    def fetch(self, codes: Sequence[str]) -> FundamentalFetchResult:
        items: Dict[str, Dict[str, object]] = {}
        errors = []
        try:
            ak = self.provider._get_akshare()
        except Exception as exc:
            message = "AKShare 财务指标失败: {}".format(exc)
            self._error(message)
            return FundamentalFetchResult(self.source, "error", {}, [message], sources=[self.source])
        for code in codes:
            try:
                frame = ak.stock_financial_analysis_indicator(symbol=code)
                _merge_fundamental_frame(items, code, frame)
            except Exception as exc:
                errors.append("{}: {}".format(code, exc))
        if errors:
            self._error("AKShare 财务指标部分失败: {}".format("; ".join(errors[:5])))
        return FundamentalFetchResult(
            self.source,
            "ok" if items else "empty",
            items,
            errors,
            sources=[self.source],
        )


class LegacyProviderFundamentalAdapter(FundamentalProviderAdapter):
    source = "legacy_provider"

    def available(self) -> bool:
        return callable(getattr(self.provider, "get_fundamental_factors", None))

    def fetch(self, codes: Sequence[str]) -> FundamentalFetchResult:
        try:
            try:
                raw_items = self.provider.get_fundamental_factors(codes=list(codes))
            except TypeError:
                raw_items = self.provider.get_fundamental_factors(list(codes))
            items = _normalize_fundamental_items(raw_items)
            return FundamentalFetchResult(self.source, "ok" if items else "empty", items, sources=[self.source])
        except Exception as exc:
            message = "provider 财务指标失败: {}".format(exc)
            self._error(message)
            return FundamentalFetchResult(self.source, "error", {}, [message], sources=[self.source])


class HistoryProviderAdapter:
    source = "history"

    def __init__(self, provider) -> None:
        self.provider = provider

    def available(self) -> bool:
        return self.provider is not None

    def fetch(self, code: str, days: int) -> HistoryLookupResult:
        raise NotImplementedError


class HistoryCacheAdapter(HistoryProviderAdapter):
    source = "history_cache"

    def available(self) -> bool:
        return callable(getattr(self.provider, "_read_history_cache", None))

    def fetch(self, code: str, days: int) -> HistoryLookupResult:
        try:
            history, fresh = self.provider._read_history_cache(code, days)
            status = "ok" if _usable_history_frame(history, days) and fresh else "stale_or_empty"
            return HistoryLookupResult(code, days, self.source, status, history, cache_fresh=bool(fresh))
        except Exception as exc:
            return HistoryLookupResult(code, days, self.source, "error", error=str(exc))


class LocalHistoryAdapter(HistoryProviderAdapter):
    source = "local_history"

    def available(self) -> bool:
        return callable(getattr(self.provider, "_load_local_history", None))

    def fetch(self, code: str, days: int) -> HistoryLookupResult:
        try:
            history = self.provider._load_local_history(code, days)
            status = "ok" if _usable_history_frame(history, days) else "empty"
            return HistoryLookupResult(code, days, self.source, status, history, fallback_used=True)
        except Exception as exc:
            return HistoryLookupResult(code, days, self.source, "error", fallback_used=True, error=str(exc))


class ProviderHistoryAdapter(HistoryProviderAdapter):
    source = "provider_history"

    def available(self) -> bool:
        return callable(getattr(self.provider, "get_history", None))

    def fetch(self, code: str, days: int) -> HistoryLookupResult:
        try:
            history = self.provider.get_history(code, days=days)
            status = "ok" if _usable_history_frame(history, days) else "empty"
            return HistoryLookupResult(code, days, self.source, status, history)
        except Exception as exc:
            return HistoryLookupResult(code, days, self.source, "error", error=str(exc))


def load_fundamentals(provider=None, codes: Iterable[str] = None, force: bool = False) -> Dict[str, object]:
    if not getattr(config, "ENABLE_FUNDAMENTALS", False):
        return {"enabled": False, "status": "disabled", "items": {}, "generated_at": ""}
    code_list = [normalize_code(code) for code in (codes or []) if normalize_code(code)]
    code_list = code_list[: max(1, int(getattr(config, "FUNDAMENTAL_FETCH_LIMIT", 200)))]
    cached = _load_cache()
    cached_items = cached.get("items") if isinstance(cached, dict) else {}
    cache_covers_request = not code_list or all(code in (cached_items or {}) for code in code_list)
    if cached and not force and cache_covers_request:
        return {**cached, "enabled": True, "status": cached.get("status", "cached")}
    adapters = fundamental_provider_adapters(provider)
    if not adapters:
        return {"enabled": True, "status": "no_provider", "items": {}, "generated_at": ""}
    if not code_list:
        return {"enabled": True, "status": "no_codes", "items": {}, "generated_at": ""}
    try:
        result = fetch_fundamentals_from_adapters(adapters, code_list)
        payload = {
            "enabled": True,
            "status": "ok" if result.items else "empty",
            "items": result.items,
            "sources": result.sources,
            "errors": result.errors,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _save_cache(payload)
        return payload
    except Exception as exc:
        return {"enabled": True, "status": "error", "error": str(exc), "items": {}, "generated_at": ""}


def attach_fundamental_factors(df: pd.DataFrame, fundamentals: Dict[str, Dict[str, object]] = None) -> pd.DataFrame:
    if df is None or df.empty or not getattr(config, "ENABLE_FUNDAMENTALS", False):
        return df
    result = df.copy()
    fundamentals = (fundamentals or {}).get("items", fundamentals or {}) if isinstance(fundamentals, dict) else {}
    if "code" in result.columns and fundamentals:
        normalized_codes = result["code"].map(normalize_code)
        for column in ALL_FUNDAMENTAL_COLUMNS:
            if column not in result.columns:
                values_by_code = {
                    normalize_code(code): values.get(column, 0.0)
                    for code, values in fundamentals.items()
                    if isinstance(values, dict)
                }
                result[column] = normalized_codes.map(values_by_code).fillna(0.0)
        for column in FUNDAMENTAL_META_COLUMNS:
            if column not in result.columns:
                values_by_code = {
                    normalize_code(code): values.get(column, "")
                    for code, values in fundamentals.items()
                    if isinstance(values, dict)
                }
                result[column] = normalized_codes.map(values_by_code).fillna("")
    for column in ALL_FUNDAMENTAL_COLUMNS:
        if column not in result.columns:
            result[column] = 0.0
        result[column] = result[column].map(coerce_number)

    roe_values = _nonzero_values(result, "roe")
    gross_values = _nonzero_values(result, "gross_margin")
    debt_values = _nonzero_values(result, "debt_ratio")
    pe_values = [value for value in finite_series(result, "pe_dynamic").tolist() if value > 0]
    pb_values = [value for value in finite_series(result, "pb").tolist() if value > 0]
    surprise_values = _nonzero_values(result, "earnings_surprise")
    revision_values = _nonzero_values(result, "rating_revision")
    degraded = not any((roe_values, gross_values, debt_values, pe_values, pb_values, surprise_values, revision_values))

    quality_scores = []
    value_scores = []
    surprise_scores = []
    revision_scores = []
    for roe, gross_margin, debt_ratio, pe_dynamic, pb_value, earnings_surprise, rating_revision in result[
        list(FUNDAMENTAL_COLUMNS)
    ].itertuples(index=False, name=None):
        quality = _neutral_if_missing(roe, roe_values) * 0.45
        quality += _neutral_if_missing(gross_margin, gross_values) * 0.35
        quality += _neutral_if_missing(debt_ratio, debt_values, higher_is_better=False) * 0.20
        pe = coerce_number(pe_dynamic)
        pb = coerce_number(pb_value)
        value = (
            percentile_score(pe, pe_values, higher_is_better=False) * 0.55
            + percentile_score(pb, pb_values, higher_is_better=False) * 0.45
            if pe > 0 and pb > 0
            else 50.0
        )
        quality_scores.append(round(quality, 2))
        value_scores.append(round(value, 2))
        surprise_scores.append(round(_neutral_if_missing(earnings_surprise, surprise_values), 2))
        revision_scores.append(round(_neutral_if_missing(rating_revision, revision_values), 2))
    result["fundamental_quality_score"] = quality_scores
    result["fundamental_value_score"] = value_scores
    result["earnings_surprise_score"] = surprise_scores
    result["rating_revision_score"] = revision_scores
    result["fundamental_status"] = "degraded" if degraded else "enabled"
    result["fundamental_degraded"] = bool(degraded)
    return result


def _nonzero_values(df: pd.DataFrame, column: str) -> list:
    return [value for value in finite_series(df, column).tolist() if abs(coerce_number(value)) > 1e-12]


def _neutral_if_missing(value: float, values: list, higher_is_better: bool = True) -> float:
    numeric = coerce_number(value)
    if not values or abs(numeric) <= 1e-12:
        return 50.0
    return percentile_score(numeric, values, higher_is_better=higher_is_better)


def fundamental_provider_adapters(provider) -> List[FundamentalProviderAdapter]:
    if provider is None:
        return []
    explicit_adapters: List[FundamentalProviderAdapter] = []
    for adapter in (
        EastmoneyFundamentalAdapter(provider),
        TushareFundamentalAdapter(provider),
        AKShareFundamentalAdapter(provider),
    ):
        if adapter.available():
            explicit_adapters.append(adapter)
    if explicit_adapters:
        return explicit_adapters
    legacy = LegacyProviderFundamentalAdapter(provider)
    return [legacy] if legacy.available() else []


def fetch_fundamentals_from_adapters(
    adapters: Sequence[FundamentalProviderAdapter],
    codes: Sequence[str],
) -> FundamentalFetchResult:
    requested = [normalize_code(code) for code in codes if normalize_code(code)]
    items: Dict[str, Dict[str, object]] = {}
    errors: List[str] = []
    sources: List[str] = []
    for adapter in adapters:
        missing = [code for code in requested if not _has_useful_fundamental(items.get(code))]
        if not missing:
            break
        result = adapter.fetch(missing)
        if result.source and result.source not in sources:
            sources.append(result.source)
        errors.extend(result.errors or [])
        for code, item in (result.items or {}).items():
            normalized = normalize_code(code)
            if normalized:
                merged = dict(items.get(normalized) or {"code": normalized})
                merged.update(item or {})
                merged["code"] = normalized
                items[normalized] = merged
    return FundamentalFetchResult(
        ",".join(sources) if sources else "",
        "ok" if items else "empty",
        items,
        errors,
        sources=sources,
    )


def history_provider_adapters(provider) -> List[HistoryProviderAdapter]:
    adapters: List[HistoryProviderAdapter] = []
    if provider is None:
        return adapters
    for adapter in (HistoryCacheAdapter(provider), LocalHistoryAdapter(provider), ProviderHistoryAdapter(provider)):
        if adapter.available():
            adapters.append(adapter)
    return adapters


def load_history_with_fallback(provider, code: str, days: int = 90) -> HistoryLookupResult:
    normalized = normalize_code(code)
    fallback = HistoryLookupResult(normalized, days, "none", "empty")
    for adapter in history_provider_adapters(provider):
        result = adapter.fetch(normalized, days)
        if result.usable and result.status == "ok":
            return result
        if result.history is not None and not result.history.empty and fallback.history.empty:
            fallback = HistoryLookupResult(
                normalized,
                days,
                result.source,
                "fallback",
                result.history,
                cache_fresh=result.cache_fresh,
                fallback_used=True,
                error=result.error,
            )
    return fallback


def _normalize_fundamental_items(raw_items) -> Dict[str, Dict[str, object]]:
    if isinstance(raw_items, pd.DataFrame):
        rows = _dataframe_records(raw_items)
    elif not raw_items:
        return {}
    elif isinstance(raw_items, dict):
        iterable = raw_items.items()
        rows = []
        for code, item in iterable:
            row = dict(item or {})
            row["code"] = code
            rows.append(row)
    else:
        rows = list(raw_items or [])
    items: Dict[str, Dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code") or row.get("ts_code") or row.get("股票代码") or row.get("证券代码"))
        if not code:
            continue
        item = {column: coerce_number(row.get(column)) for column in ALL_FUNDAMENTAL_COLUMNS}
        item.update(_fundamental_metadata(row))
        items[code] = item
    return items


def _dataframe_records(frame: pd.DataFrame) -> List[Dict[str, object]]:
    if frame is None or frame.empty:
        return []
    columns = list(frame.columns)
    return [dict(zip(columns, values)) for values in frame.itertuples(index=False, name=None)]


def _first_dataframe_record(frame: pd.DataFrame) -> Dict[str, object]:
    if frame is None or frame.empty:
        return {}
    columns = list(frame.columns)
    values = next(frame.itertuples(index=False, name=None), None)
    return dict(zip(columns, values)) if values is not None else {}


def _merge_fundamental_frame(items: Dict[str, Dict[str, object]], code: str, frame: pd.DataFrame) -> None:
    row = _first_dataframe_record(frame)
    if row:
        _merge_fundamental_row(items, code, row)


def _merge_fundamental_row(items: Dict[str, Dict[str, object]], code: str, row: Dict[str, object]) -> None:
    normalized = normalize_code(code)
    if not normalized:
        return
    item = items.setdefault(normalized, {"code": normalized})
    mappings = {
        "roe": ("roe", "ROE", "净资产收益率", "净资产收益率(%)", "roe_dt"),
        "gross_margin": ("gross_margin", "销售毛利率", "毛利率", "grossprofit_margin"),
        "debt_ratio": ("debt_ratio", "资产负债率", "资产负债率(%)", "debt_to_assets"),
        "pe_dynamic": ("pe_dynamic", "动态市盈率", "市盈率", "PE", "pe"),
        "pb": ("pb", "市净率", "PB"),
        "earnings_surprise": ("earnings_surprise", "业绩变动幅度", "净利润增长率", "q_profit_yoy", "or_yoy"),
        "rating_revision": ("rating_revision", "rating_revision", "预测调整", "盈利预测调整"),
        "revenue_yoy": ("revenue_yoy", "营业收入同比增长", "营收同比", "or_yoy"),
        "net_profit_yoy": ("net_profit_yoy", "净利润同比增长", "净利润同比", "q_profit_yoy"),
        "operating_cashflow": ("operating_cashflow", "经营活动现金流量净额", "经营现金流", "n_cashflow_act"),
        "operating_cashflow_yoy": ("operating_cashflow_yoy", "经营现金流同比", "经营活动现金流同比"),
        "free_cashflow": ("free_cashflow", "自由现金流", "fcf"),
        "current_ratio": ("current_ratio", "流动比率"),
        "receivables_yoy": ("receivables_yoy", "应收账款同比", "应收同比"),
        "inventory_yoy": ("inventory_yoy", "存货同比"),
        "goodwill_ratio": ("goodwill_ratio", "商誉占比"),
        "interest_bearing_debt_ratio": ("interest_bearing_debt_ratio", "有息负债率"),
    }
    for target, columns in mappings.items():
        value = _first_mapping_value(row, columns)
        if value not in (None, ""):
            item[target] = coerce_number(value)
    item.update({key: value for key, value in _fundamental_metadata(row).items() if value not in (None, "")})


def _fundamental_metadata(row: Dict[str, object]) -> Dict[str, object]:
    return {
        "announcement_time": _first_mapping_value(
            row,
            (
                "announcement_time",
                "announce_time",
                "announce_date",
                "ann_date",
                "f_ann_date",
                "公告发布时间",
                "公告日期",
            ),
        )
        or "",
        "report_period": _first_mapping_value(
            row,
            ("report_period", "end_date", "报告期", "报告日期"),
        )
        or "",
        "source_timestamp": _first_mapping_value(
            row,
            ("source_timestamp", "updated_at", "update_time", "数据时间"),
        )
        or "",
    }


def _first_mapping_value(row: Dict[str, object], columns: Tuple[str, ...]):
    for column in columns:
        value = row.get(column)
        if pd.notna(value) and str(value).strip() not in ("", "-", "--", "nan"):
            return value
    return None


def _has_useful_fundamental(item: Dict[str, object]) -> bool:
    if not item:
        return False
    return any(abs(coerce_number(item.get(column))) > 1e-12 for column in FUNDAMENTAL_COLUMNS)


def _usable_history_frame(history: pd.DataFrame, days: int) -> bool:
    return history is not None and not history.empty and len(history) >= min(days, 30)


def _to_ts_code(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("600", "601", "603", "605", "688")):
        return "{}.SH".format(code)
    return "{}.SZ".format(code)


def _load_cache() -> Dict[str, object]:
    path = getattr(config, "FUNDAMENTAL_CACHE_PATH", ".runtime/fundamentals.json")
    try:
        if not os.path.exists(path):
            return {}
        max_age = int(getattr(config, "FUNDAMENTAL_CACHE_HOURS", 24)) * 3600
        if time.time() - os.path.getmtime(path) > max_age:
            return {}
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_cache(payload: Dict[str, object]) -> None:
    path = getattr(config, "FUNDAMENTAL_CACHE_PATH", ".runtime/fundamentals.json")
    try:
        atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    except Exception:
        return
