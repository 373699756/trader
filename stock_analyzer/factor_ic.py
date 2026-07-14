import json
from datetime import datetime
from typing import Dict, Iterable, List

import pandas as pd

from . import config
from .normalization import coerce_number
from .runtime_json import atomic_write_json


DEFAULT_FACTOR_KEYS = (
    "momentum_score",
    "trend_score",
    "liquidity_score",
    "execution_score",
    "fundamental_quality_score",
    "fundamental_value_score",
    "earnings_surprise_score",
    "rating_revision_score",
)


def compute_factor_ic(samples: Iterable[Dict[str, object]], factor_keys: Iterable[str] = None) -> Dict[str, object]:
    keys = list(factor_keys or DEFAULT_FACTOR_KEYS)
    rows: List[Dict[str, float]] = []
    for sample in samples or []:
        raw = sample.get("raw") if isinstance(sample.get("raw"), dict) else sample
        item = {
            "signal_date": str(sample.get("signal_date") or ""),
            "return": coerce_number(sample.get("primary_return_net")),
        }
        for key in keys:
            item[key] = coerce_number(raw.get(key) if isinstance(raw, dict) else sample.get(key))
        rows.append(item)
    if not rows:
        return {
            "factor_count": len(keys),
            "sample_count": 0,
            "daily_count": 0,
            "method": "daily_cross_section_spearman_rank_ic",
            "ic": {},
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    df = pd.DataFrame(rows)
    if not df["signal_date"].astype(str).str.strip().any():
        return _compute_pooled_factor_ic(df, keys)
    result = {}
    for key in keys:
        daily = []
        sample_count = 0
        for signal_date, group in df.groupby("signal_date", sort=True):
            if not str(signal_date or "").strip():
                continue
            valid = group[[key, "return"]].dropna()
            valid = valid[valid[key].abs() > 1e-12]
            if len(valid) < 3:
                continue
            ranked_factor = valid[key].rank(method="average")
            ranked_return = valid["return"].rank(method="average")
            ic = ranked_factor.corr(ranked_return)
            daily.append({"signal_date": str(signal_date), "ic": coerce_number(ic), "sample_count": int(len(valid))})
            sample_count += int(len(valid))
        if not daily:
            result[key] = {
                "ic": 0.0,
                "sample_count": sample_count,
                "daily_count": 0,
                "status": "insufficient",
                "method": "daily_cross_section_spearman_rank_ic",
            }
            continue
        values = [item["ic"] for item in daily]
        mean_ic = _avg(values)
        std_ic = _std(values)
        result[key] = {
            "ic": round(mean_ic, 4),
            "ic_mean": round(mean_ic, 4),
            "icir": round(mean_ic / std_ic, 4) if std_ic > 1e-12 else 0.0,
            "positive_ic_rate": round(sum(1 for value in values if value > 0) / len(values), 4),
            "sample_count": sample_count,
            "daily_count": len(daily),
            "status": "ok",
            "method": "daily_cross_section_spearman_rank_ic",
            "windows": _ic_windows(daily, windows=(20, 60, 120)),
        }
    return {
        "factor_count": len(keys),
        "sample_count": len(rows),
        "daily_count": len({row["signal_date"] for row in rows if row.get("signal_date")}),
        "method": "daily_cross_section_spearman_rank_ic",
        "ic": result,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _compute_pooled_factor_ic(df: pd.DataFrame, keys: List[str]) -> Dict[str, object]:
    result = {}
    for key in keys:
        valid = df[[key, "return"]].dropna()
        valid = valid[valid[key].abs() > 1e-12]
        if len(valid) < 3:
            result[key] = {"ic": 0.0, "sample_count": int(len(valid)), "status": "insufficient"}
            continue
        ranked_factor = valid[key].rank(method="average")
        ranked_return = valid["return"].rank(method="average")
        ic = ranked_factor.corr(ranked_return)
        result[key] = {
            "ic": round(coerce_number(ic), 4),
            "sample_count": int(len(valid)),
            "status": "ok",
            "method": "pooled_spearman_rank_ic_legacy",
        }
    return {
        "factor_count": len(keys),
        "sample_count": len(df),
        "daily_count": 0,
        "method": "pooled_spearman_rank_ic_legacy",
        "ic": result,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _ic_windows(daily: List[Dict[str, object]], windows=(20, 60, 120)) -> Dict[str, object]:
    ordered = sorted(daily, key=lambda item: item.get("signal_date") or "")
    payload = {}
    for window in windows:
        values = [item["ic"] for item in ordered[-int(window):]]
        payload[str(window)] = {
            "ic": round(_avg(values), 4) if values else 0.0,
            "daily_count": len(values),
            "positive_ic_rate": round(sum(1 for value in values if value > 0) / len(values), 4) if values else 0.0,
        }
    return payload


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _avg(values)
    return (sum((value - mean) ** 2 for value in values) / (len(values) - 1)) ** 0.5


def save_factor_ic(payload: Dict[str, object]) -> None:
    path = getattr(config, "FACTOR_IC_PATH", ".runtime/factor_ic.json")
    try:
        atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    except Exception:
        return


def load_factor_ic() -> Dict[str, object]:
    path = getattr(config, "FACTOR_IC_PATH", ".runtime/factor_ic.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
