from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Dict, Iterable

from . import config


SWITCH_NAMES = (
    "ALLOW_SLOW_QUOTE_FALLBACK",
    "CALIBRATE_TOMORROW_DIRECTION_FOCUSED",
    "CALIBRATE_USE_SORTINO",
    "CALIBRATE_USE_TIME_DECAY",
    "DEEPSEEK_CASCADE_FILTER_ENABLED",
    "DEEPSEEK_SCHEDULE_ENABLED",
    "DEEPSEEK_SHADOW_ONLY",
    "DEEPSEEK_WRITE_ALPHA_ZERO",
    "EVENT_RISK_HARD_FILTER",
    "HISTORY_FACTORS_FETCH_ON_REQUEST",
    "META_LABELING_ENFORCE_ACTION",
    "PAPER_TRADING_SPREAD_CAPITAL_BY_HOLDING_DAYS",
    "PRODUCTION_FREEZE_ENABLED",
    "RISK_BLACKLIST_HARD_FILTER",
    "STRATEGY_VALIDATION_REQUIRE_POSITIVE_CI",
    "USE_SMOOTH_PENALTY",
    "VALIDATION_ALLOW_LOCAL_QUOTE_SNAPSHOT",
    "VALIDATION_AUTO_SNAPSHOT_ENABLED",
    "VALIDATION_AUTO_UPDATE_ENABLED",
)

OUTPUT_FINGERPRINT_FIELDS = (
    "code",
    "rank",
    "score",
    "predicted_net_return",
    "ranking_source",
    "tier",
    "execution_allowed",
)


def frozen_manifest() -> Dict[str, object]:
    manifest = getattr(config, "PRODUCTION_BASELINE_MANIFEST", {})
    return json.loads(json.dumps(manifest, ensure_ascii=False)) if isinstance(manifest, dict) else {}


def manifest_fingerprint(manifest: Dict[str, object] = None) -> str:
    payload = manifest if isinstance(manifest, dict) else frozen_manifest()
    return _sha256(payload)


def production_baseline_id() -> str:
    manifest = frozen_manifest()
    name = str(manifest.get("baseline_name") or "production_baseline")
    return "{}__{}".format(name, manifest_fingerprint(manifest)[:12])


def runtime_switches() -> Dict[str, bool]:
    names = set(SWITCH_NAMES)
    names.update(name for name in dir(config) if name.startswith("ENABLE_") or name.endswith("_ENABLED"))
    return {
        name: bool(getattr(config, name))
        for name in sorted(names)
        if hasattr(config, name) and isinstance(getattr(config, name), bool)
    }


def production_baseline_status() -> Dict[str, object]:
    manifest = frozen_manifest()
    expected_switches = manifest.get("switches") if isinstance(manifest.get("switches"), dict) else {}
    expected_config = manifest.get("locked_config") if isinstance(manifest.get("locked_config"), dict) else {}
    actual_switches = runtime_switches()
    drift = []
    for name, expected in expected_switches.items():
        actual = actual_switches.get(name)
        if actual is None:
            drift.append({"key": name, "expected": bool(expected), "actual": None, "reason": "missing_switch"})
        elif bool(actual) != bool(expected):
            drift.append({"key": name, "expected": bool(expected), "actual": bool(actual), "reason": "switch_mismatch"})
    for name, expected in expected_config.items():
        actual = getattr(config, name, None)
        if actual != expected:
            drift.append({"key": name, "expected": expected, "actual": actual, "reason": "config_mismatch"})
    unregistered = sorted(set(actual_switches) - set(expected_switches))
    drift.extend(
        {"key": name, "expected": None, "actual": actual_switches[name], "reason": "unregistered_switch"}
        for name in unregistered
    )
    return {
        "baseline_id": production_baseline_id(),
        "baseline_name": manifest.get("baseline_name", ""),
        "manifest_fingerprint": manifest_fingerprint(manifest),
        "manifest_path": getattr(config, "PRODUCTION_BASELINE_MANIFEST_PATH", ""),
        "freeze_enabled": bool(getattr(config, "PRODUCTION_FREEZE_ENABLED", False)),
        "status": "frozen" if not drift else "drift_detected",
        "drift": drift,
        "switches": actual_switches,
        "switches_fingerprint": _sha256(actual_switches),
        "research": manifest.get("research") or {},
        "strategy_versions": manifest.get("strategy_versions") or {},
        "ranking": manifest.get("ranking") or {},
        "candidate_filters": manifest.get("candidate_filters") or {},
        "exit_rules": manifest.get("exit_rules") or {},
        "weights_fingerprint": _sha256(manifest.get("weights") or {}),
    }


def attach_generation_provenance(
    meta: Dict[str, object],
    strategy_name: str,
    rows: Iterable[Dict[str, object]],
    candidates=None,
) -> Dict[str, object]:
    rows = list(rows or [])
    baseline = production_baseline_status()
    manifest = frozen_manifest()
    ranking_config = (manifest.get("ranking") or {}).get(strategy_name) or {}
    expected_return = meta.get("expected_return_ranking") if isinstance(meta.get("expected_return_ranking"), dict) else {}
    ranking_field = (
        "predicted_net_return"
        if expected_return.get("status") == "active"
        else str(ranking_config.get("field") or "score")
    )
    strategy_version = str(
        meta.get("strategy_version")
        or (manifest.get("strategy_versions") or {}).get(strategy_name)
        or ""
    )
    research = manifest.get("research") if isinstance(manifest.get("research"), dict) else {}
    input_fingerprint = dataframe_fingerprint(candidates)
    output_fingerprint = recommendation_output_fingerprint(rows)
    decision_context = _decision_context(meta)
    decision_context_fingerprint = _sha256(decision_context)
    input_as_of = _frame_as_of(candidates)
    replay_context = {
        "strategy": strategy_name,
        "strategy_version": strategy_version,
        "input_as_of": input_as_of,
        "market_filter": str(meta.get("market_filter") or "all"),
        "requested_top_n": int(meta.get("top_n") or 0),
        "input_fingerprint": input_fingerprint,
        "output_fingerprint": output_fingerprint,
        "decision_context_fingerprint": decision_context_fingerprint,
    }
    provenance = {
        "schema_version": 1,
        "baseline_id": baseline["baseline_id"],
        "baseline_status": baseline["status"],
        "manifest_fingerprint": baseline["manifest_fingerprint"],
        "strategy": strategy_name,
        "strategy_version": strategy_version,
        "ranking_field": ranking_field,
        "ranking_direction": str(ranking_config.get("direction") or "descending"),
        "switches": baseline["switches"],
        "switches_fingerprint": baseline["switches_fingerprint"],
        "weights_fingerprint": baseline["weights_fingerprint"],
        "production_top_k": int(research.get("production_top_k") or 5),
        "sensitivity_top_k": list(research.get("sensitivity_top_k") or [3, 5, 10]),
        "top_k_selection_policy": str(research.get("selection_policy") or ""),
        "candidate_filter_version": str((manifest.get("candidate_filters") or {}).get("version") or ""),
        "exit_rule_version": str((manifest.get("exit_rules") or {}).get("version") or ""),
        "input_fingerprint": input_fingerprint,
        "output_fingerprint": output_fingerprint,
        "decision_context": decision_context,
        "decision_context_fingerprint": decision_context_fingerprint,
        "replay_context": replay_context,
        "replay_key": _sha256({"baseline_id": baseline["baseline_id"], **replay_context}),
        "drift": baseline["drift"],
    }
    meta["generation"] = provenance
    return provenance


def verify_generation_replay(
    generation: Dict[str, object],
    rows: Iterable[Dict[str, object]],
    candidates=None,
    decision_meta: Dict[str, object] = None,
) -> Dict[str, object]:
    generation = generation if isinstance(generation, dict) else {}
    checks = {
        "baseline_id": (
            str(generation.get("baseline_id") or ""),
            production_baseline_id(),
        ),
        "input_fingerprint": (
            str(generation.get("input_fingerprint") or ""),
            dataframe_fingerprint(candidates),
        ),
        "output_fingerprint": (
            str(generation.get("output_fingerprint") or ""),
            recommendation_output_fingerprint(rows),
        ),
    }
    if decision_meta is not None:
        checks["decision_context_fingerprint"] = (
            str(generation.get("decision_context_fingerprint") or ""),
            _sha256(_decision_context(decision_meta)),
        )
    mismatches = [
        {"field": field, "expected": expected, "actual": actual}
        for field, (expected, actual) in checks.items()
        if expected != actual
    ]
    return {
        "ok": not mismatches,
        "status": "reproduced" if not mismatches else "mismatch",
        "replay_key": generation.get("replay_key", ""),
        "mismatches": mismatches,
    }


def dataframe_fingerprint(frame) -> str:
    if frame is None:
        return _sha256({"columns": [], "records": []})
    if hasattr(frame, "columns") and hasattr(frame, "to_dict"):
        columns = sorted(str(column) for column in frame.columns)
        records = []
        for raw in frame.to_dict("records"):
            records.append({column: _json_value(raw.get(column)) for column in columns})
        return _sha256({"columns": columns, "records": records})
    return _sha256(_json_value(frame))


def recommendation_output_fingerprint(rows: Iterable[Dict[str, object]]) -> str:
    output = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        output.append({key: _json_value(row.get(key)) for key in OUTPUT_FINGERPRINT_FIELDS})
    return _sha256(output)


def _frame_as_of(frame) -> str:
    attrs = getattr(frame, "attrs", {}) if frame is not None else {}
    if isinstance(attrs, dict):
        for key in ("quote_timestamp", "snapshot_mtime", "market_data_cutoff"):
            value = str(attrs.get(key) or "").strip()
            if value:
                return value
    return ""


def _decision_context(meta: Dict[str, object]) -> Dict[str, object]:
    meta = meta if isinstance(meta, dict) else {}
    keys = (
        "analysis_window",
        "display_limit",
        "display_min_score",
        "intraday_relaxed_mode",
        "market_filter",
        "market_regime",
        "min_score",
        "primary_min_score",
        "top_n",
        "validation_gate",
    )
    return {key: _json_value(meta.get(key)) for key in keys if key in meta}


def _sha256(value) -> str:
    raw = json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except Exception:
            pass
    return str(value)
