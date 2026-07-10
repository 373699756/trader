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
        item = {"return": coerce_number(sample.get("primary_return_net"))}
        for key in keys:
            item[key] = coerce_number(raw.get(key) if isinstance(raw, dict) else sample.get(key))
        rows.append(item)
    if not rows:
        return {"factor_count": len(keys), "sample_count": 0, "ic": {}, "generated_at": datetime.now().isoformat(timespec="seconds")}
    df = pd.DataFrame(rows)
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
        result[key] = {"ic": round(coerce_number(ic), 4), "sample_count": int(len(valid)), "status": "ok"}
    return {
        "factor_count": len(keys),
        "sample_count": len(rows),
        "ic": result,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


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
