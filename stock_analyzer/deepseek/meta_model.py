from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from typing import Dict, Iterable, List

from .. import config
from ..normalization import coerce_number
from ..runtime_json import atomic_write_json
from ..strategies.types import storage_strategy_name
from .feature_schema import FEATURE_SCHEMA_VERSION, prompt_version


META_SCHEMA_VERSION = "deepseek_meta_linear_v3"
META_FEATURES = (
    "local_score",
    "deepseek_missing",
    "event_direction",
    "event_strength",
    "event_reliability",
    "novelty",
    "priced_in",
    "overnight_risk",
    "regulatory_risk",
    "theme_truth",
    "uncertainty",
    "horizon_match",
    "abstain",
    "value_quality_assessment",
    "financial_profit_trend",
    "financial_cashflow_trend",
    "market_flow_health",
    "industry_outlook",
    "policy_relevance",
    "research_risk_level",
    "research_confidence",
)


def artifact_path(strategy_name: str) -> str:
    strategy = storage_strategy_name(strategy_name)
    root = str(getattr(config, "DEEPSEEK_META_ARTIFACT_DIR", ".runtime") or ".runtime")
    return os.path.join(root, "deepseek_meta_{}.json".format(strategy))


def load_meta_artifact(strategy_name: str, path: str = "") -> Dict[str, object]:
    try:
        with open(path or artifact_path(strategy_name), "r", encoding="utf-8") as handle:
            artifact = json.load(handle)
    except Exception:
        return {}
    return artifact if validate_meta_artifact(artifact, strategy_name) else {}


def validate_meta_artifact(artifact: Dict[str, object], strategy_name: str) -> bool:
    if not isinstance(artifact, dict):
        return False
    strategy = storage_strategy_name(strategy_name)
    return bool(
        artifact.get("schema_version") == META_SCHEMA_VERSION
        and artifact.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and artifact.get("strategy_name") == strategy
        and artifact.get("prompt_version") == prompt_version(strategy)
        and artifact.get("feature_model_name")
        == str(getattr(config, "DEEPSEEK_FEATURE_MODEL", "deepseek-v4-flash"))
        and tuple(artifact.get("feature_names") or ()) == META_FEATURES
        and isinstance(artifact.get("coefficients"), dict)
    )


def meta_feature_vector(row: Dict[str, object], feature: Dict[str, object]) -> Dict[str, float]:
    value_quality = feature.get("value_quality") if isinstance(feature.get("value_quality"), dict) else {}
    financial = feature.get("financial_health") if isinstance(feature.get("financial_health"), dict) else {}
    market_flow = feature.get("market_flow") if isinstance(feature.get("market_flow"), dict) else {}
    industry = feature.get("industry_policy") if isinstance(feature.get("industry_policy"), dict) else {}
    risk = feature.get("risk_assessment") if isinstance(feature.get("risk_assessment"), dict) else {}
    confidences = [
        coerce_number(section.get("confidence"))
        for section in (value_quality, financial, market_flow, industry, risk)
    ]
    return {
        "local_score": coerce_number(row.get("local_score"), coerce_number(row.get("score"), 50.0)) / 100.0,
        "deepseek_missing": 1.0 if feature.get("deepseek_missing") else 0.0,
        "event_direction": coerce_number(feature.get("event_direction")) / 2.0,
        "event_strength": coerce_number(feature.get("event_strength")) / 100.0,
        "event_reliability": coerce_number(feature.get("event_reliability")) / 100.0,
        "novelty": coerce_number(feature.get("novelty")) / 100.0,
        "priced_in": coerce_number(feature.get("priced_in"), 50.0) / 100.0,
        "overnight_risk": coerce_number(feature.get("overnight_risk"), 50.0) / 100.0,
        "regulatory_risk": coerce_number(feature.get("regulatory_risk"), 50.0) / 100.0,
        "theme_truth": coerce_number(feature.get("theme_truth"), 50.0) / 100.0,
        "uncertainty": coerce_number(feature.get("uncertainty"), 100.0) / 100.0,
        "horizon_match": 1.0 if feature.get("horizon_match") else 0.0,
        "abstain": 1.0 if feature.get("abstain") else 0.0,
        "value_quality_assessment": _ordinal(value_quality.get("assessment"), positive="positive", negative="negative"),
        "financial_profit_trend": _ordinal(financial.get("profit_trend"), positive="improving", negative="deteriorating"),
        "financial_cashflow_trend": _ordinal(financial.get("cashflow_trend"), positive="improving", negative="deteriorating"),
        "market_flow_health": _ordinal(market_flow.get("flow_health"), positive="healthy", negative="unhealthy"),
        "industry_outlook": _ordinal(industry.get("industry_outlook"), positive="growing", negative="contracting"),
        "policy_relevance": {"direct": 1.0, "indirect": 0.5, "none": -1.0}.get(str(industry.get("policy_relevance")), 0.0),
        "research_risk_level": {"low": 0.0, "medium": 0.5, "high": 1.0}.get(str(risk.get("risk_level")), 0.5),
        "research_confidence": sum(confidences) / max(1, len(confidences)) / 100.0,
    }


def _ordinal(value: object, *, positive: str, negative: str) -> float:
    text = str(value or "")
    if text == positive:
        return 1.0
    if text == negative:
        return -1.0
    return 0.0


def score_meta_features(
    row: Dict[str, object],
    feature: Dict[str, object],
    artifact: Dict[str, object],
) -> Dict[str, object]:
    if not artifact:
        return {"available": False, "production_eligible": False}
    vector = meta_feature_vector(row, feature)
    linear = coerce_number(artifact.get("intercept"), 0.0)
    coefficients = artifact.get("coefficients") or {}
    for name in META_FEATURES:
        linear += coerce_number(coefficients.get(name), 0.0) * vector[name]
    probability = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, linear))))
    expected_increment = coerce_number(artifact.get("return_intercept"), 0.0)
    return_coefficients = artifact.get("return_coefficients") or {}
    for name in META_FEATURES:
        expected_increment += coerce_number(return_coefficients.get(name), 0.0) * vector[name]
    cap = max(0.0, coerce_number(getattr(config, "DEEPSEEK_META_RETURN_INCREMENT_CAP_PCT", 1.0), 1.0))
    expected_increment = max(-cap, min(cap, expected_increment))
    gates = artifact.get("promotion_gates") or {}
    return {
        "available": True,
        "shadow_probability": round(probability, 6),
        "shadow_expected_return_increment_pct": round(expected_increment, 4),
        "production_eligible": bool(gates.get("passed")),
        "artifact_id": str(artifact.get("artifact_id") or ""),
        "artifact_trained_through": str(artifact.get("trained_through") or ""),
    }


def build_meta_artifact(
    strategy_name: str,
    *,
    coefficients: Dict[str, float],
    intercept: float = 0.0,
    return_coefficients: Dict[str, float] = None,
    return_intercept: float = 0.0,
    trained_through: str = "",
    sample_count: int = 0,
    coverage_days: int = 0,
    validation: Dict[str, object] = None,
) -> Dict[str, object]:
    strategy = storage_strategy_name(strategy_name)
    validation = dict(validation or {})
    gates = promotion_gates(sample_count, coverage_days, validation)
    payload = {
        "schema_version": META_SCHEMA_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "prompt_version": prompt_version(strategy),
        "feature_model_name": str(getattr(config, "DEEPSEEK_FEATURE_MODEL", "deepseek-v4-flash")),
        "strategy_name": strategy,
        "feature_names": list(META_FEATURES),
        "coefficients": {name: coerce_number((coefficients or {}).get(name), 0.0) for name in META_FEATURES},
        "intercept": coerce_number(intercept),
        "return_coefficients": {
            name: coerce_number((return_coefficients or {}).get(name), 0.0) for name in META_FEATURES
        },
        "return_intercept": coerce_number(return_intercept),
        "trained_through": str(trained_through or ""),
        "sample_count": int(sample_count),
        "coverage_days": int(coverage_days),
        "validation": validation,
        "promotion_gates": gates,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    payload["artifact_id"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:20]
    return payload


def promotion_gates(sample_count: int, coverage_days: int, validation: Dict[str, object]) -> Dict[str, object]:
    g1_checks = {
        "sample_count_ge_300": int(sample_count) >= 300,
        "coverage_days_ge_60": int(coverage_days) >= 60,
        "feature_coverage_ge_80pct": coerce_number(validation.get("feature_coverage_pct"), 0.0) >= 80.0,
        "regime_coverage_complete": bool(validation.get("regime_coverage_complete")),
    }
    g2_checks = {
        "g1_passed": all(g1_checks.values()),
        "cumulative_increment_positive": coerce_number(validation.get("incremental_return_total"), 0.0) > 0.0,
        "increment_ci95_low_positive": coerce_number(validation.get("incremental_return_ci95_low"), -1.0) > 0.0,
        "max_drawdown_not_worse": bool(validation.get("max_drawdown_not_worse")),
        "increment_diversification_complete": bool(validation.get("increment_diversification_complete")),
    }
    g4_checks = {
        "g2_passed": all(g2_checks.values()),
        "coverage_days_ge_120": int(coverage_days) >= 120,
    }
    return {
        "passed": all(g4_checks.values()),
        "stage": "g4_formal_meta" if all(g4_checks.values()) else "g2_experiment" if all(g2_checks.values()) else "g1_shadow" if all(g1_checks.values()) else "g0_engineering",
        "g1": {"passed": all(g1_checks.values()), "checks": g1_checks},
        "g2": {"passed": all(g2_checks.values()), "checks": g2_checks},
        "g4": {"passed": all(g4_checks.values()), "checks": g4_checks},
    }


def save_meta_artifact(artifact: Dict[str, object], path: str = "") -> str:
    target = path or artifact_path(str(artifact.get("strategy_name") or ""))
    atomic_write_json(target, artifact, ensure_ascii=False, indent=2)
    return target
