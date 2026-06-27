import argparse
import json
from typing import Dict

import pandas as pd

from . import config
from .normalization import coerce_number
from .scoring import ALPHALITE_SIGNAL_COLUMNS


def factor_coverage(candidates: pd.DataFrame) -> Dict[str, object]:
    if candidates is None or candidates.empty:
        return {
            "row_count": 0,
            "avg_data_coverage": 0.0,
            "columns": {},
            "history_factors_enabled": bool(config.ENABLE_HISTORY_FACTORS),
            "degraded": True,
        }
    total = len(candidates)
    columns = {}
    row_coverages = []
    coverage_series = (
        candidates["alphalite_coverage"]
        if "alphalite_coverage" in candidates.columns
        else pd.Series([0.0] * total)
    )
    for _, row in candidates.iterrows():
        row_coverages.append(max(0.0, min(1.0, coerce_number(row.get("alphalite_coverage")))))
    for column in ALPHALITE_SIGNAL_COLUMNS:
        nonzero = coverage_series.map(lambda value: coerce_number(value) > 0).sum()
        columns[column] = round(nonzero / total, 4) if total else 0.0
    avg = sum(row_coverages) / len(row_coverages) if row_coverages else 0.0
    return {
        "row_count": total,
        "avg_data_coverage": round(avg, 4),
        "columns": columns,
        "history_factors_enabled": bool(config.ENABLE_HISTORY_FACTORS),
        "degraded": avg < coerce_number(getattr(config, "CALIBRATE_MIN_COVERAGE", 0.5), 0.5),
    }


def main() -> int:
    from .factors import build_alphalite_factors, merge_alphalite
    from .providers import MarketDataProvider
    from .scoring import prepare_candidates

    parser = argparse.ArgumentParser(description="检查 AlphaLite 历史因子覆盖率")
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()

    provider = MarketDataProvider()
    quotes = provider.get_realtime_quotes()
    candidates = prepare_candidates(quotes).head(max(1, args.limit))
    if config.ENABLE_HISTORY_FACTORS:
        history_by_code = {}
        for code in candidates["code"].tolist():
            history = provider.get_history(code, days=90)
            if history is not None and not history.empty:
                history_by_code[code] = history
        candidates = merge_alphalite(candidates, build_alphalite_factors(history_by_code))
    print(json.dumps(factor_coverage(candidates), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
