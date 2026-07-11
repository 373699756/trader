"""Stress-scenario helpers for validation samples."""

import json
import os
from typing import Dict, List

from . import config
from .normalization import coerce_number


def load_stress_scenarios(path: str = None) -> List[Dict[str, object]]:
    path = path or getattr(config, "STRESS_TEST_SCENARIOS_PATH", ".runtime/stress_scenarios.json")
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return []
    if isinstance(payload, dict):
        payload = payload.get("scenarios") or []
    return [item for item in payload if isinstance(item, dict)]


def stress_test_samples(
    samples: List[Dict[str, object]],
    scenarios: List[Dict[str, object]] = None,
    return_key: str = "primary_return_net",
) -> Dict[str, object]:
    rows = [sample for sample in samples or [] if isinstance(sample, dict) and sample.get("signal_date")]
    scenarios = scenarios if scenarios is not None else load_stress_scenarios()
    results = []
    for scenario in scenarios or []:
        selected = _scenario_rows(rows, scenario)
        if not selected:
            continue
        returns = [coerce_number(row.get(return_key), coerce_number(row.get("primary_return_net"))) for row in selected]
        wins = sum(1 for value in returns if value > 0)
        total_return = sum(returns)
        results.append(
            {
                "scenario": str(scenario.get("name") or "unnamed"),
                "sample_count": len(selected),
                "win_rate": round(wins / len(selected) * 100.0, 4),
                "avg_return": round(total_return / len(selected), 4),
                "max_single_loss": round(min(returns), 4),
                "total_return": round(total_return, 4),
            }
        )
    worst = min(results, key=lambda item: item["total_return"]) if results else None
    return {
        "enabled": bool(getattr(config, "ENABLE_STRESS_TEST", False)),
        "scenario_count": len(results),
        "scenarios": results,
        "worst_scenario": worst,
        "status": "ready" if results else "no_matching_scenarios",
    }


def _scenario_rows(rows: List[Dict[str, object]], scenario: Dict[str, object]) -> List[Dict[str, object]]:
    ranges = scenario.get("dates") or scenario.get("date_ranges") or []
    if not ranges:
        return []
    selected = []
    for row in rows:
        date_value = _compact_date(row.get("signal_date"))
        if not date_value:
            continue
        if any(_in_range(date_value, date_range) for date_range in ranges):
            selected.append(row)
    return selected


def _in_range(date_value: str, date_range) -> bool:
    try:
        start, end = date_range
    except Exception:
        return False
    start_value = _compact_date(start)
    end_value = _compact_date(end)
    if not start_value or not end_value:
        return False
    return start_value <= date_value <= end_value


def _compact_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10].replace("-", "")
