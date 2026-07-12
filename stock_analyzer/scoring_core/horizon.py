from __future__ import annotations

from datetime import datetime
from typing import Dict

import pandas as pd

from .. import config
from ..normalization import coerce_number


__all__ = [
    "_horizon_meta",
    "_horizon_row",
]


def _horizon_meta(
    top_n: int,
    market_filter: str,
    candidate_count: int,
    strategy_version: str,
    strategy_label: str,
) -> Dict[str, object]:
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": candidate_count,
        "top_n": top_n,
        "market_filter": market_filter,
        "strategy_version": strategy_version,
        "strategy_label": strategy_label,
    }


def _horizon_row(row: pd.Series, scores: Dict[str, object]) -> Dict[str, object]:
    item = {
        "code": row["code"],
        "name": str(row.get("name", "")),
        "market": row.get("market", "main"),
        "market_label": config.MARKET_LABELS.get(row.get("market", "main"), "主板"),
        "industry": str(row.get("industry", "") or ""),
        "price": round(coerce_number(row.get("price")), 3),
        "pct_chg": round(coerce_number(row.get("pct_chg")), 2),
        "volume_ratio": round(coerce_number(row.get("volume_ratio")), 2),
        "turnover_rate": round(coerce_number(row.get("turnover_rate")), 2),
        "turnover": round(coerce_number(row.get("turnover")), 2),
        "sixty_day_pct": round(coerce_number(row.get("sixty_day_pct")), 2),
        "ytd_pct": round(coerce_number(row.get("ytd_pct")), 2),
        "amplitude": round(coerce_number(row.get("amplitude")), 2),
    }
    for key, value in scores.items():
        if key in ("reasons", "horizon", "theme", "breakout_20d"):
            item[key] = value
        elif isinstance(value, (int, float)):
            item[key] = round(max(0.0, min(100.0, value)), 2) if key == "score" else round(value, 2)
        else:
            item[key] = value
    return item
