from __future__ import annotations

import copy
import json
import os
from typing import Dict, Tuple

from .. import config


_DEFAULT_WEIGHTS = {
    "today_term": {
        "momentum": 0.35,
        "liquidity": 0.25,
        "industry": 0.10,
        "sentiment": 0.20,
        "risk_guard": 0.10,
        "reversal_tilt": 0.0,
    },
    "tomorrow_picks": {
        "liquidity": 0.30,
        "momentum": 0.20,
        "execution": 0.15,
        "tail_setup": 0.15,
        "historical_edge": 0.20,
    },
    "swing_picks": {
        "momentum": 0.30,
        "trend": 0.25,
        "liquidity": 0.20,
        "execution": 0.15,
        "not_overextended": 0.10,
    },
    "regime_profiles": {
        "risk_on": {
            "momentum": 1.12,
            "trend": 1.08,
            "breakout": 1.16,
            "volume": 1.08,
            "lowvol": 0.88,
            "quality": 0.92,
        },
        "risk_off": {
            "momentum": 0.82,
            "trend": 0.94,
            "breakout": 0.78,
            "volume": 0.88,
            "lowvol": 1.18,
            "quality": 1.16,
            "liquidity": 1.08,
        },
        "balanced": {
            "momentum": 0.96,
            "trend": 1.0,
            "breakout": 0.94,
            "volume": 1.0,
            "lowvol": 1.06,
            "quality": 1.04,
        },
    },
    "decision_score": {
        "base_score": 0.32,
        "execution_score": 0.20,
        "quality_score": 0.18,
        "rule_consistency_score": 0.12,
        "committee_score": 0.10,
        "risk_guard": 0.08,
    },
}


STRATEGY_COMBINERS = {
    "today_term": {
        "apply_damp": True,
        "terms": (
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "industry_score", "weight_key": "industry"},
            {"component": "sentiment_score", "weight_key": "sentiment"},
            {"component": "risk_guard_score", "weight_key": "risk_guard", "regime_key": "quality"},
        ),
    },
    "tomorrow_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "historical_edge_score", "weight_key": "historical_edge", "regime_key": "quality"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
            {"component": "tail_setup_score", "weight_key": "tail_setup", "regime_key": "quality"},
        ),
    },
    "swing_picks": {
        "apply_damp": True,
        "terms": (
            {"component": "momentum_score", "weight_key": "momentum", "regime_key": "momentum"},
            {"component": "trend_score", "weight_key": "trend", "regime_key": "trend"},
            {"component": "liquidity_score", "weight_key": "liquidity", "regime_key": "liquidity"},
            {"component": "execution_score", "weight_key": "execution", "regime_key": "quality"},
            {"component": "not_overextended_score", "weight_key": "not_overextended", "regime_key": "quality"},
        ),
    },
}


COMPONENT_FACTOR_KEYS = {
    "momentum_score": "momentum_score",
    "trend_score": "trend_score",
    "liquidity_score": "liquidity_score",
    "execution_score": "execution_score",
    "quality_proxy_score": "fundamental_quality_score",
    "value_score": "fundamental_value_score",
    "fundamental_quality_score": "fundamental_quality_score",
    "fundamental_value_score": "fundamental_value_score",
    "earnings_surprise_score": "earnings_surprise_score",
    "rating_revision_score": "rating_revision_score",
}


_DEFAULT_THRESHOLDS = {
    "verdict": {"strong_buy": 80.0, "buy": 65.0, "watch": 50.0, "reduce": 35.0},
    "min_data_coverage": 0.5,
    "overheat_damp_floor": 0.6,
}


def _load_weight_overrides() -> Tuple[Dict[str, object], Dict[str, object]]:
    weights = copy.deepcopy(_DEFAULT_WEIGHTS)
    thresholds = copy.deepcopy(_DEFAULT_THRESHOLDS)
    if bool(getattr(config, "PRODUCTION_FREEZE_ENABLED", False)):
        manifest = getattr(config, "PRODUCTION_BASELINE_MANIFEST", {}) or {}
        frozen_weights = manifest.get("weights") if isinstance(manifest.get("weights"), dict) else {}
        frozen_thresholds = manifest.get("thresholds") if isinstance(manifest.get("thresholds"), dict) else {}
        for group, values in frozen_weights.items():
            if isinstance(values, dict):
                weights.setdefault(group, {}).update(values)
        for key, value in frozen_thresholds.items():
            if isinstance(value, dict) and isinstance(thresholds.get(key), dict):
                thresholds[key].update(value)
            else:
                thresholds[key] = value
        return weights, thresholds

    path = getattr(config, "WEIGHTS_OVERRIDE_PATH", os.path.join(".runtime", "weights.json"))
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for group, values in (payload.get("weights") or {}).items():
                if isinstance(values, dict):
                    weights.setdefault(group, {}).update(values)
            for key, value in (payload.get("thresholds") or {}).items():
                default = thresholds.get(key)
                if isinstance(default, dict):
                    if isinstance(value, dict):
                        default.update(value)
                    continue
                thresholds[key] = value
            cov = thresholds.get("min_data_coverage")
            if not isinstance(cov, (int, float)) or not (0.0 <= cov <= 1.0):
                thresholds["min_data_coverage"] = _DEFAULT_THRESHOLDS["min_data_coverage"]
            floor = thresholds.get("overheat_damp_floor")
            if not isinstance(floor, (int, float)) or not (0.0 <= floor <= 1.0):
                thresholds["overheat_damp_floor"] = _DEFAULT_THRESHOLDS["overheat_damp_floor"]
    except Exception:
        return copy.deepcopy(_DEFAULT_WEIGHTS), copy.deepcopy(_DEFAULT_THRESHOLDS)
    return weights, thresholds


WEIGHTS, THRESHOLDS = _load_weight_overrides()
