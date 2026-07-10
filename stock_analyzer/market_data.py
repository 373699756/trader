"""Market data maintenance CLI for the local daily_bars store."""

import argparse
import json
import time
from datetime import datetime, timedelta
from typing import Dict, Iterable, List

import pandas as pd
import requests

from . import config
from .daily_data import DailyMarketDataStore, supported_stock_rows
from .normalization import normalize_code, rename_known_columns


def load_stock_universe(include_st: bool = False) -> List[Dict[str, object]]:
    """Load the A-share universe from AKShare and normalize it for local storage."""
    import akshare as ak

    errors = []
    try:
        rows = supported_stock_rows(_fetch_eastmoney_universe(), include_st=include_st)
        if rows:
            return rows
    except Exception as exc:  # pragma: no cover - network/provider dependent
        errors.append("eastmoney: {}".format(exc))
    for func_name in ("stock_info_a_code_name", "stock_zh_a_spot_em"):
        try:
            df = getattr(ak, func_name)()
        except Exception as exc:  # pragma: no cover - network/provider dependent
            errors.append("{}: {}".format(func_name, exc))
            continue
        rows = _stock_rows_from_frame(df)
        rows = supported_stock_rows(rows, include_st=include_st)
        if rows:
            return rows
    raise RuntimeError("无法加载股票列表: {}".format("; ".join(errors)))


def summarize_market_data(db_path: str = "") -> Dict[str, object]:
    return DailyMarketDataStore(db_path or config.MARKET_DATA_DB_PATH).summary()


def download_market_data(
    codes: Iterable[str] = (),
    db_path: str = "",
    days: int = 720,
    end_date: str = "",
    limit: int = 0,
    force: bool = False,
    include_st: bool = False,
    sleep_seconds: float = 0.15,
) -> Dict[str, object]:
    """Download daily raw and qfq bars into market_data.sqlite3.

    Existing rows are skipped unless ``force`` is enabled or the code has no
    usable local data. Use ``limit`` for safe incremental batches.
    """
    import akshare as ak

    store = DailyMarketDataStore(db_path or config.MARKET_DATA_DB_PATH)
    requested_codes = [normalize_code(code) for code in codes if normalize_code(code)]
    if requested_codes:
        stock_rows = [{"code": code, "name": "", "is_active": True} for code in requested_codes]
    else:
        stock_rows = load_stock_universe(include_st=include_st)
    stock_rows = supported_stock_rows(stock_rows, include_st=include_st)
    stock_rows = _dedupe_rows(stock_rows)
    end = _date_key(end_date) or _latest_complete_date()
    if not force:
        stock_rows.sort(key=lambda item: (_is_current(store, item.get("code"), end), normalize_code(item.get("code"))))
    if limit > 0:
        stock_rows = stock_rows[:limit]
    store.upsert_stock_meta(stock_rows)

    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=max(days * 2, 120))).strftime("%Y%m%d")
    result = {
        "ok": True,
        "db_path": store.db_path,
        "requested": len(stock_rows),
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "start_date": start,
        "end_date": end,
        "errors": [],
    }

    for row in stock_rows:
        code = normalize_code(row.get("code"))
        name = str(row.get("name", ""))
        latest = store.latest_trade_date(code)
        if not force and latest and latest >= end:
            result["skipped"] += 1
            continue
        try:
            raw_history = _fetch_history(ak, code, start, end, adjust="")
            qfq_history = _fetch_history(ak, code, start, end, adjust="qfq")
            row_count = store.upsert_bars(code, raw_history, qfq_history)
            if row_count <= 0:
                raise RuntimeError("历史行情为空")
            latest_after = store.latest_trade_date(code)
            store.record_status(code, name, "ok", last_trade_date=latest_after, row_count=row_count)
            result["downloaded"] += 1
        except Exception as exc:  # pragma: no cover - network/provider dependent
            result["failed"] += 1
            message = str(exc)
            result["errors"].append({"code": code, "name": name, "error": message})
            result["errors"] = result["errors"][-20:]
            store.record_status(code, name, "failed", error=message)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    result["summary"] = store.summary()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="下载/检查本地 A 股日线历史数据")
    parser.add_argument("--summary", action="store_true", help="只输出本地 market_data.sqlite3 覆盖率")
    parser.add_argument("--download", action="store_true", help="下载缺失或过期的日线历史")
    parser.add_argument("--codes", default="", help="逗号分隔股票代码；留空则尝试全市场")
    parser.add_argument("--days", type=int, default=720, help="回溯自然日窗口，默认 720")
    parser.add_argument("--end-date", default="", help="结束日期 YYYYMMDD；默认最近完整交易日近似")
    parser.add_argument("--limit", type=int, default=0, help="限制本次处理股票数，便于分批")
    parser.add_argument("--force", action="store_true", help="即使已有最新数据也重新抓取")
    parser.add_argument("--include-st", action="store_true", help="股票池包含 ST/退市名称")
    parser.add_argument("--sleep", type=float, default=0.15, help="每只股票下载间隔秒数")
    parser.add_argument("--db-path", default="", help="覆盖 MARKET_DATA_DB_PATH")
    args = parser.parse_args()

    if args.summary or not args.download:
        print(json.dumps(summarize_market_data(args.db_path), ensure_ascii=False, indent=2))
        if not args.download:
            return 0

    codes = _parse_codes(args.codes)
    result = download_market_data(
        codes=codes,
        db_path=args.db_path,
        days=args.days,
        end_date=args.end_date,
        limit=max(0, args.limit),
        force=args.force,
        include_st=args.include_st,
        sleep_seconds=max(0.0, args.sleep),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def _stock_rows_from_frame(df: pd.DataFrame) -> List[Dict[str, object]]:
    if df is None or df.empty:
        return []
    normalized = rename_known_columns(df.copy())
    if "code" not in normalized.columns:
        return []
    if "name" not in normalized.columns:
        normalized["name"] = ""
    return normalized[["code", "name"]].to_dict("records")


def _fetch_history(ak, code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    errors = []
    try:
        return _fetch_eastmoney_history(code, start_date, end_date, adjust=adjust)
    except Exception as exc:
        errors.append("eastmoney: {}".format(exc))
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
    except Exception as exc:
        errors.append("akshare: {}".format(exc))
    else:
        if df is not None and not df.empty:
            return df
        errors.append("akshare: empty")
    try:
        return _fetch_sina_history(code, start_date, end_date)
    except Exception as exc:
        errors.append("sina: {}".format(exc))
        raise RuntimeError("; ".join(errors))


def _fetch_eastmoney_history(code: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
    payload = _eastmoney_get(
        path="/api/qt/stock/kline/get",
        params={
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",
            "fqt": _eastmoney_adjust_code(adjust),
            "secid": _eastmoney_secid(code),
            "beg": start_date,
            "end": end_date,
        },
        hosts=("push2his.eastmoney.com", "82.push2his.eastmoney.com", "7.push2his.eastmoney.com"),
    )
    klines = ((payload.get("data") or {}).get("klines") or [])
    rows = []
    for item in klines:
        parts = str(item).split(",")
        if len(parts) < 11:
            continue
        rows.append(
            {
                "trade_date": parts[0],
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "turnover": parts[6],
                "pct_chg": parts[8],
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _fetch_sina_history(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    datalen = max(120, min(2000, (end - start).days + 10))
    symbol = "{}{}".format("sh" if normalize_code(code).startswith(("5", "6", "9")) else "sz", normalize_code(code))
    with requests.get(
        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=10,
    ) as response:
        response.raise_for_status()
        rows = response.json()
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "day" not in df.columns:
        return pd.DataFrame()
    df["trade_date"] = df["day"].astype(str).str.replace("-", "", regex=False)
    df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)].copy()
    if df.empty:
        return pd.DataFrame()
    for column in ("open", "high", "low", "close", "volume"):
        if column not in df.columns:
            df[column] = 0.0
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0)
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["turnover"] = 0.0
    df["pct_chg"] = df["close"].pct_change().fillna(0.0) * 100
    return df[["trade_date", "open", "close", "high", "low", "volume", "turnover", "pct_chg"]]


def _fetch_eastmoney_universe() -> List[Dict[str, object]]:
    rows = []
    first = _eastmoney_clist_page(1)
    data = first.get("data") or {}
    diff = data.get("diff") or []
    rows.extend(_eastmoney_spot_rows(diff))
    page_size = max(1, len(diff))
    total = int(data.get("total") or len(diff))
    total_pages = max(1, (total + page_size - 1) // page_size)
    for page in range(2, total_pages + 1):
        try:
            payload = _eastmoney_clist_page(page)
        except Exception:
            break
        rows.extend(_eastmoney_spot_rows((payload.get("data") or {}).get("diff") or []))
    return rows


def _eastmoney_clist_page(page: int) -> Dict[str, object]:
    return _eastmoney_get(
        path="/api/qt/clist/get",
        params={
            "pn": str(page),
            "pz": "500",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
            "fields": (
                "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
                "f20,f21,f23,f24,f25,f22,f11,f62,f100,f128,f136,f115,f152"
            ),
        },
        hosts=("push2.eastmoney.com", "82.push2.eastmoney.com", "7.push2.eastmoney.com"),
    )


def _eastmoney_get(path: str, params: Dict[str, str], hosts: Iterable[str]) -> Dict[str, object]:
    last_error = None
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    for host in hosts:
        for scheme in ("https", "http"):
            for trust_env in (False, True):
                try:
                    with requests.Session() as session:
                        session.trust_env = trust_env
                        with session.get(
                            "{}://{}{}".format(scheme, host, path),
                            params=params,
                            headers=headers,
                            timeout=10,
                        ) as response:
                            response.raise_for_status()
                            payload = response.json()
                except Exception as exc:
                    last_error = exc
                    continue
                if payload.get("data"):
                    return payload
                last_error = RuntimeError("empty data")
    raise RuntimeError(str(last_error) if last_error else "request failed")


def _eastmoney_spot_rows(rows: Iterable[object]) -> List[Dict[str, object]]:
    result = []
    for row in rows:
        if isinstance(row, dict):
            code = row.get("f12") or row.get("代码") or row.get("code")
            name = row.get("f14") or row.get("名称") or row.get("name")
            market_cap = str(row.get("f20") or row.get("f21") or "").strip()
        else:
            continue
        if code and market_cap not in ("", "-", "--"):
            result.append({"code": normalize_code(code), "name": str(name or "")})
    return result


def _eastmoney_secid(code: str) -> str:
    normalized = normalize_code(code)
    market_id = "1" if normalized.startswith(("5", "6", "9")) else "0"
    return "{}.{}".format(market_id, normalized)


def _eastmoney_adjust_code(adjust: str) -> str:
    if adjust == "qfq":
        return "1"
    if adjust == "hfq":
        return "2"
    return "0"


def _dedupe_rows(rows: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    result = []
    seen = set()
    for row in rows:
        code = normalize_code(row.get("code"))
        if not code or code in seen:
            continue
        seen.add(code)
        item = dict(row)
        item["code"] = code
        result.append(item)
    return result


def _parse_codes(value: str) -> List[str]:
    if not value:
        return []
    return list(dict.fromkeys(normalize_code(code) for code in value.replace("，", ",").split(",") if code.strip()))


def _date_key(value: str) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return text
    return ""


def _is_current(store: DailyMarketDataStore, code: str, end_date: str) -> int:
    latest = store.latest_trade_date(normalize_code(code))
    return 1 if latest and latest >= end_date else 0


def _latest_complete_date() -> str:
    now = datetime.now()
    if now.hour < 17:
        now = now - timedelta(days=1)
    while now.weekday() >= 5:
        now = now - timedelta(days=1)
    return now.strftime("%Y%m%d")


if __name__ == "__main__":
    raise SystemExit(main())
