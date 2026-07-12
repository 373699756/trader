import argparse
import json
from typing import Dict

import pandas as pd

from . import config
from .normalization import coerce_number
from .scoring_core.constants import ALPHALITE_SIGNAL_COLUMNS


def factor_coverage(candidates: pd.DataFrame) -> Dict[str, object]:
    if candidates is None or candidates.empty:
        return {
            "row_count": 0,
            "avg_data_coverage": 0.0,
            "alphalite_ready_ratio": 0.0,
            "alphalite_not_ready_ratio": 1.0,
            "alphalite_zero_coverage_ratio": 1.0,
            "columns": {},
            "history_factors_enabled": bool(config.ENABLE_HISTORY_FACTORS),
            "degraded": True,
            "alerts": [],
        }
    total = len(candidates)
    columns = {}
    row_coverages = []
    ready_series = (
        candidates["alphalite_factor_ready"].map(lambda value: coerce_number(value) > 0)
        if "alphalite_factor_ready" in candidates.columns
        else pd.Series([False] * total, index=candidates.index)
    )
    coverage_series = (
        candidates["alphalite_coverage"].map(lambda value: max(0.0, min(1.0, coerce_number(value))))
        if "alphalite_coverage" in candidates.columns
        else pd.Series([0.0] * total, index=candidates.index)
    )
    for _, row in candidates.iterrows():
        row_coverages.append(max(0.0, min(1.0, coerce_number(row.get("alphalite_coverage")))))
    for column in ALPHALITE_SIGNAL_COLUMNS:
        if column not in candidates.columns:
            columns[column] = 0.0
            continue
        nonzero = (
            ready_series
            & candidates[column].map(lambda value: abs(coerce_number(value)) > 1e-12)
        ).sum()
        columns[column] = round(nonzero / total, 4) if total else 0.0
    avg = sum(row_coverages) / len(row_coverages) if row_coverages else 0.0
    ready_ratio = round(float(ready_series.sum()) / total, 4) if total else 0.0
    zero_ready_ratio = round(1.0 - ready_ratio, 4) if total else 0.0
    zero_coverage_ratio = round(float((coverage_series <= 1e-12).sum()) / total, 4) if total else 0.0
    alert_threshold = coerce_number(getattr(config, "FACTOR_COVERAGE_ALERT_ZERO_RATIO", 0.30), 0.30)
    alerts = []
    if zero_coverage_ratio > alert_threshold:
        alerts.append(
            {
                "code": "alphalite_coverage_zero",
                "level": "critical",
                "message": "历史因子覆盖率为0的候选比例过高，短线/波段历史因子正在降级。",
                "ratio": zero_coverage_ratio,
                "threshold": round(alert_threshold, 4),
            }
        )
    if zero_ready_ratio > alert_threshold:
        alerts.append(
            {
                "code": "alphalite_factor_not_ready",
                "level": "critical",
                "message": "alphalite_factor_ready=0 的候选比例过高，需检查日线下载和因子快照任务。",
                "ratio": zero_ready_ratio,
                "threshold": round(alert_threshold, 4),
            }
        )
    degraded = (
        avg < coerce_number(getattr(config, "CALIBRATE_MIN_COVERAGE", 0.5), 0.5)
        or zero_coverage_ratio > alert_threshold
        or zero_ready_ratio > alert_threshold
    )
    return {
        "row_count": total,
        "avg_data_coverage": round(avg, 4),
        "alphalite_ready_ratio": ready_ratio,
        "alphalite_not_ready_ratio": zero_ready_ratio,
        "alphalite_zero_coverage_ratio": zero_coverage_ratio,
        "columns": columns,
        "history_factors_enabled": bool(config.ENABLE_HISTORY_FACTORS),
        "degraded": degraded,
        "alerts": alerts,
    }


def main() -> int:
    from .factors import build_alphalite_factors, merge_alphalite
    from .providers import MarketDataProvider
    from .scoring_core.candidate_filters import prepare_candidates

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
