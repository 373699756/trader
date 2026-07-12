"""Model-score ensemble helpers.

The ensemble layer is shadow-only by default: it computes an independent
blended score and agreement diagnostic without changing production ranking.
"""

import json
from typing import Dict, List

from . import config
from .normalization import coerce_number


DEFAULT_MODEL_WEIGHTS = {
    "price_volume": 0.35,
    "expected_return": 0.25,
    "event": 0.20,
    "probability": 0.10,
    "meta": 0.10,
}


def ensemble_score(model_scores: Dict[str, object], model_weights: Dict[str, object] = None) -> Dict[str, object]:
    weights = _normalized_weights(model_weights or _configured_weights() or DEFAULT_MODEL_WEIGHTS)
    scores = {key: max(0.0, min(100.0, coerce_number(model_scores.get(key), 50.0))) for key in weights}
    total_weight = sum(weights.values()) or 1.0
    blended = sum(scores[key] * weights[key] for key in weights) / total_weight
    values = list(scores.values())
    dispersion = max(values) - min(values) if values else 0.0
    agreement = max(0.0, min(1.0, 1.0 - dispersion / 100.0))
    return {
        "ensemble_score": round(blended, 4),
        "agreement": round(agreement, 4),
        "dispersion": round(dispersion, 4),
        "model_scores": {key: round(value, 4) for key, value in scores.items()},
        "model_weights": {key: round(value, 4) for key, value in weights.items()},
    }


def attach_ensemble_score(rows: List[Dict[str, object]], model_weights: Dict[str, object] = None) -> None:
    for row in rows or []:
        result = ensemble_score(row_model_scores(row), model_weights=model_weights)
        result["enabled"] = False
        result["mode"] = "shadow_only"
        row["ensemble"] = result
        row["ensemble_score"] = result["ensemble_score"]


def row_model_scores(row: Dict[str, object]) -> Dict[str, float]:
    probability = row.get("calibrated_probability")
    if probability is None:
        probability = row.get("p_win")
    meta_confidence = row.get("meta_confidence")
    if meta_confidence is None and isinstance(row.get("meta_labeling"), dict):
        meta_confidence = row["meta_labeling"].get("confidence")
    return {
        "price_volume": coerce_number(row.get("score"), 50.0),
        "expected_return": coerce_number(row.get("rank_score"), row.get("score", 50.0)),
        "event": coerce_number(row.get("event_alpha_score"), 50.0),
        "probability": _probability_to_score(probability),
        "meta": _probability_to_score(meta_confidence),
    }


def _probability_to_score(value) -> float:
    probability = coerce_number(value, None)
    if probability is None:
        return 50.0
    if probability <= 1.0:
        return max(0.0, min(100.0, probability * 100.0))
    return max(0.0, min(100.0, probability))


def _configured_weights() -> Dict[str, object]:
    raw = str(getattr(config, "ENSEMBLE_MODEL_WEIGHTS", "") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalized_weights(values: Dict[str, object]) -> Dict[str, float]:
    cleaned = {}
    for key, value in (values or {}).items():
        weight = coerce_number(value, 0.0)
        if weight > 0:
            cleaned[str(key)] = weight
    if not cleaned:
        cleaned = dict(DEFAULT_MODEL_WEIGHTS)
    total = sum(cleaned.values()) or 1.0
    return {key: value / total for key, value in cleaned.items()}
