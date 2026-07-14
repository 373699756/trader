from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Dict, List

from . import config
from .pit_snapshot import (
    DAILY_PROXY_REPLAY,
    INTRADAY_PIT_REPLAY,
    LEGACY_BASELINE,
    REAL_FORWARD,
    normalize_sample_type,
)


REQUIRED_FIELDS = (
    "experiment_id",
    "hypothesis",
    "unique_change",
    "training_window",
    "test_window",
    "primary_metric",
    "risk_constraints",
    "experiment_family",
    "data_cutoff",
    "sample_type",
    "production_baseline_id",
    "strategy_version",
    "execution_policy_version",
    "feature_schema",
    "candidate_parameters",
    "trial_count",
    "cost_scenarios",
    "pass_fail_conditions",
    "result",
    "decision",
)

PROMOTION_SAMPLE_TYPES = {
    REAL_FORWARD,
    INTRADAY_PIT_REPLAY,
    DAILY_PROXY_REPLAY,
    LEGACY_BASELINE,
}


def register_experiment(record: Dict[str, object], path: str = "") -> Dict[str, object]:
    item = validate_experiment(record)
    target = path or getattr(config, "EXPERIMENT_REGISTRY_PATH", "experiments/registry.jsonl")
    existing = list_experiments(target)
    if any(row.get("experiment_id") == item["experiment_id"] for row in existing):
        raise ValueError("duplicate experiment_id: {}".format(item["experiment_id"]))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return item


def validate_experiment(record: Dict[str, object]) -> Dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError("experiment record must be an object")
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    if missing:
        raise ValueError("missing experiment fields: {}".format(", ".join(missing)))
    item = dict(record)
    item["experiment_id"] = str(item.get("experiment_id") or "").strip()
    if not item["experiment_id"]:
        raise ValueError("experiment_id must not be empty")
    for field in ("hypothesis", "primary_metric", "experiment_family", "strategy_version", "execution_policy_version"):
        item[field] = str(item.get(field) or "").strip()
        if not item[field]:
            raise ValueError("{} must not be empty".format(field))
    unique_change = item.get("unique_change")
    if isinstance(unique_change, list):
        if len(unique_change) != 1 or not str(unique_change[0]).strip():
            raise ValueError("unique_change must contain exactly one change")
    elif not str(unique_change or "").strip():
        raise ValueError("unique_change must describe exactly one change")
    for window_name in ("training_window", "test_window"):
        if not isinstance(item.get(window_name), dict):
            raise ValueError("{} must be an object".format(window_name))
        if not any(key in item[window_name] for key in ("start", "end", "status")):
            raise ValueError("{} must declare start/end/status".format(window_name))
    data_cutoff = item.get("data_cutoff")
    if not isinstance(data_cutoff, (str, dict)) or not data_cutoff:
        raise ValueError("data_cutoff must be a non-empty string or object")
    sample_type = normalize_sample_type(item.get("sample_type"))
    if sample_type not in PROMOTION_SAMPLE_TYPES:
        raise ValueError("sample_type must be one of real_forward/intraday_pit_replay/daily_proxy_replay/legacy_baseline")
    item["sample_type"] = sample_type
    item["production_baseline_id"] = str(item.get("production_baseline_id") or "").strip()
    if not item["production_baseline_id"]:
        raise ValueError("production_baseline_id must not be empty")
    existing_baseline = str(item.get("baseline_id") or "").strip()
    if existing_baseline and existing_baseline != item["production_baseline_id"]:
        raise ValueError("baseline_id must match production_baseline_id")
    item["baseline_id"] = item["production_baseline_id"]
    feature_schema = item.get("feature_schema")
    if not isinstance(feature_schema, dict) or not feature_schema:
        raise ValueError("feature_schema must be a non-empty object")
    candidate_parameters = item.get("candidate_parameters")
    if not isinstance(candidate_parameters, list) or not candidate_parameters:
        raise ValueError("candidate_parameters must list every attempted configuration")
    trial_count = int(item.get("trial_count") or 0)
    if trial_count < len(candidate_parameters) or trial_count <= 0:
        raise ValueError("trial_count must cover all candidate_parameters")
    item["trial_count"] = trial_count
    cost_scenarios = item.get("cost_scenarios")
    if not isinstance(cost_scenarios, (list, dict)) or not cost_scenarios:
        raise ValueError("cost_scenarios must not be empty")
    pass_fail_conditions = item.get("pass_fail_conditions")
    if not isinstance(pass_fail_conditions, (list, dict)) or not pass_fail_conditions:
        raise ValueError("pass_fail_conditions must not be empty")
    constraints = item.get("risk_constraints")
    if not isinstance(constraints, (list, dict)) or not constraints:
        raise ValueError("risk_constraints must not be empty")
    top_k = item.get("top_k") or {
        "production": 5,
        "sensitivity": [3, 5, 10],
        "selection_locked": True,
    }
    if int(top_k.get("production") or 0) != 5:
        raise ValueError("production Top-K is frozen at 5")
    if sorted({int(value) for value in top_k.get("sensitivity") or []}) != [3, 5, 10]:
        raise ValueError("Top-K sensitivity must report K=3/5/10")
    if not bool(top_k.get("selection_locked")):
        raise ValueError("Top-K selection must remain locked")
    item["top_k"] = top_k
    if not isinstance(item.get("result"), dict):
        raise ValueError("result must be an object")
    item["decision"] = str(item.get("decision") or "").strip()
    if not item["decision"]:
        raise ValueError("decision must not be empty")
    item.setdefault("registered_at", datetime.now().isoformat(timespec="seconds"))
    item.setdefault("strategy", "tomorrow_picks")
    return item


def list_experiments(path: str = "") -> List[Dict[str, object]]:
    target = path or getattr(config, "EXPERIMENT_REGISTRY_PATH", "experiments/registry.jsonl")
    if not os.path.exists(target):
        return []
    rows = []
    with open(target, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid registry JSON at line {}: {}".format(line_number, exc)) from exc
            rows.append(item)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage the pre-registered strategy experiment ledger.")
    parser.add_argument("command", choices=("list", "register"))
    parser.add_argument("--record", help="JSON file containing one experiment record")
    parser.add_argument("--path", default="")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(json.dumps(list_experiments(args.path), ensure_ascii=False, indent=2))
        return 0
    if not args.record:
        parser.error("--record is required for register")
    with open(args.record, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    print(json.dumps(register_experiment(record, args.path), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
