"""Lightweight meta-labeling confidence model.

The first production-safe step is intentionally dependency-free: it estimates
whether a selected signal is likely to be correct from historical validation
samples and attaches shadow confidence fields to recommendation rows.
"""

from typing import Dict, List

from . import config
from .normalization import coerce_number


def train_meta_label_model(
    strategy: str,
    samples: List[Dict[str, object]],
    min_samples: int = None,
) -> Dict[str, object]:
    valid = []
    for sample in samples or []:
        if not isinstance(sample, dict) or sample.get("primary_return_net") is None:
            continue
        raw = sample.get("raw") if isinstance(sample.get("raw"), dict) else {}
        score = coerce_number(raw.get("score"), coerce_number(sample.get("stored_score"), None))
        if score is None:
            continue
        risk_penalty = _sample_risk_penalty(raw)
        primary_return = coerce_number(sample.get("primary_return_net"))
        valid.append(
            {
                "score": score,
                "risk_penalty": risk_penalty,
                "primary_return": primary_return,
                "win": primary_return > 0,
            }
        )
    min_samples = int(min_samples or getattr(config, "META_LABELING_MIN_SAMPLES", 50))
    if len(valid) < min_samples:
        return {
            "is_fitted": False,
            "strategy": strategy,
            "status": "insufficient_samples",
            "sample_count": len(valid),
            "min_samples": min_samples,
        }
    win_count = sum(1 for item in valid if item["win"])
    return {
        "is_fitted": True,
        "strategy": strategy,
        "status": "ready",
        "sample_count": len(valid),
        "positive_rate": round(win_count / len(valid), 4),
        "score_buckets": _bucket_stats(valid, "score", _score_edges()),
        "risk_buckets": _bucket_stats(valid, "risk_penalty", _risk_edges()),
    }


def predict_meta_confidence(row: Dict[str, object], model: Dict[str, object]) -> Dict[str, object]:
    if not model or not model.get("is_fitted"):
        return {
            "status": "insufficient_samples",
            "confidence": None,
            "action": "unknown",
            "reason": "元标签样本不足",
        }
    score = coerce_number(row.get("score"), row.get("stored_score"))
    risk_penalty = coerce_number(row.get("risk_penalty"), None)
    if risk_penalty is None:
        risk_penalty = coerce_number(((row.get("sell_risk") or {}).get("score")), 50.0) / 10.0
    score_label = _bucket_label(score, _score_edges())
    risk_label = _bucket_label(risk_penalty, _risk_edges())
    score_bucket = (model.get("score_buckets") or {}).get(score_label) or {}
    risk_bucket = (model.get("risk_buckets") or {}).get(risk_label) or {}
    overall = coerce_number(model.get("positive_rate"), 0.5)
    score_prob = coerce_number(score_bucket.get("win_probability"), overall)
    risk_prob = coerce_number(risk_bucket.get("win_probability"), overall)
    calibrated = coerce_number(row.get("calibrated_probability"), None)
    if calibrated is None:
        calibrated = coerce_number(row.get("p_win"), None)
    confidence = overall * 0.30 + score_prob * 0.35 + risk_prob * 0.25
    if calibrated is not None:
        confidence = confidence * 0.85 + max(0.0, min(1.0, calibrated)) * 0.15
    confidence = round(max(0.05, min(0.95, confidence)), 4)
    full_threshold = coerce_number(getattr(config, "META_LABELING_FULL_THRESHOLD", 0.65), 0.65)
    reduced_threshold = coerce_number(getattr(config, "META_LABELING_REDUCED_THRESHOLD", 0.50), 0.50)
    if confidence >= full_threshold:
        action = "full"
        label = "高置信"
        scale = 1.0
    elif confidence >= reduced_threshold:
        action = "reduced"
        label = "降仓"
        scale = round(0.5 + 0.5 * confidence, 4)
    else:
        action = "skip"
        label = "跳过"
        scale = 0.0
    return {
        "status": "ready",
        "confidence": confidence,
        "action": action,
        "label": label,
        "position_scale": scale,
        "score_bucket": score_label,
        "risk_bucket": risk_label,
        "sample_count": model.get("sample_count", 0),
        "score_bucket_sample_count": score_bucket.get("sample_count", 0),
        "risk_bucket_sample_count": risk_bucket.get("sample_count", 0),
    }


def apply_meta_labeling(
    rows: List[Dict[str, object]],
    model: Dict[str, object],
    enforce: bool = False,
) -> None:
    for row in rows or []:
        prediction = predict_meta_confidence(row, model)
        prediction["enabled"] = bool(enforce)
        row["meta_labeling"] = prediction
        if prediction.get("confidence") is not None:
            row["meta_confidence"] = prediction["confidence"]
        if enforce:
            _apply_meta_action(row, prediction)


def _apply_meta_action(row: Dict[str, object], prediction: Dict[str, object]) -> None:
    trade_action = row.get("trade_action")
    if not isinstance(trade_action, dict):
        return
    action = prediction.get("action")
    scale = coerce_number(prediction.get("position_scale"), 1.0)
    base = coerce_number(trade_action.get("position_size"), 0.0)
    trade_action.setdefault("base_meta_position_size", base)
    trade_action["meta_position_scale"] = round(scale, 4)
    if action == "skip":
        trade_action["action"] = "watch_only"
        trade_action["position_size"] = 0.0
        trade_action["meta_labeling_reason"] = "元标签置信度不足，降为观察"
    elif action == "reduced":
        trade_action["position_size"] = round(base * scale, 4)
        trade_action["meta_labeling_reason"] = "元标签建议降仓"


def _bucket_stats(items: List[Dict[str, object]], key: str, edges: List[float]) -> Dict[str, Dict[str, object]]:
    buckets: Dict[str, Dict[str, object]] = {}
    for item in items:
        value = coerce_number(item.get(key), None)
        if value is None:
            continue
        label = _bucket_label(value, edges)
        bucket = buckets.setdefault(label, {"sample_count": 0, "win_count": 0, "return_total": 0.0})
        bucket["sample_count"] += 1
        bucket["win_count"] += 1 if item.get("win") else 0
        bucket["return_total"] += coerce_number(item.get("primary_return"))
    result = {}
    for label, bucket in buckets.items():
        count = int(bucket["sample_count"] or 0)
        if count <= 0:
            continue
        result[label] = {
            "sample_count": count,
            "win_probability": round(bucket["win_count"] / count, 4),
            "avg_return": round(bucket["return_total"] / count, 4),
        }
    return result


def _bucket_label(value, edges: List[float]) -> str:
    number = coerce_number(value, 0.0)
    low = 0.0
    for high in edges:
        if number < high:
            return "{}-{}".format(_format_edge(low), _format_edge(high))
        low = high
    return "{}+".format(_format_edge(edges[-1]))


def _format_edge(value: float) -> str:
    return str(int(value)) if abs(value - int(value)) < 1e-9 else str(round(value, 2))


def _score_edges() -> List[float]:
    return [45.0, 55.0, 65.0, 75.0, 101.0]


def _risk_edges() -> List[float]:
    return [3.0, 6.0, 10.0, 999.0]


def _sample_risk_penalty(raw: Dict[str, object]) -> float:
    if raw.get("risk_penalty") is not None:
        return coerce_number(raw.get("risk_penalty"), 0.0)
    parts = raw.get("risk_penalty_parts")
    if isinstance(parts, dict):
        return sum(coerce_number(value) for value in parts.values())
    return 0.0
