import json
import math
import os
import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from . import config
from .normalization import coerce_number
from .runtime_json import atomic_write_json


MIN_SHADOW_SAMPLES = 20
MIN_READY_DAYS = 60
NEAREST_SCORE_FRACTION = 0.35
MODEL_VERSION = "nearest_feature_net_return_v4"
ARTIFACT_SCHEMA_VERSION = 1
ARTIFACT_TYPE = "expected_return_model"
FEATURE_DISTANCE_MIN_FIELDS = 3
FEATURE_SPECS = (
    ("score", 100.0, 1.8),
    ("risk_penalty", 20.0, 1.4),
    ("liquidity_score", 100.0, 1.0),
    ("momentum_score", 100.0, 1.0),
    ("historical_edge_score", 100.0, 1.0),
    ("execution_score", 100.0, 0.9),
    ("tail_setup_score", 100.0, 0.9),
    ("trend_score", 100.0, 0.9),
    ("not_overextended_score", 100.0, 0.8),
    ("regime_bonus", 10.0, 0.5),
    ("overheat_damp", 1.0, 0.5),
    ("volatility_20d", 10.0, 0.7),
    ("turnover_rate", 20.0, 0.5),
    ("volume_ratio", 5.0, 0.5),
    ("amplitude", 15.0, 0.5),
    ("sixty_day_pct", 100.0, 0.4),
    ("ytd_pct", 150.0, 0.4),
)


def expected_return_artifact_path(strategy: str, artifact_dir: str = "") -> str:
    directory = artifact_dir or str(getattr(config, "EXPECTED_RETURN_ARTIFACT_DIR", ".runtime/expected_return_models"))
    return os.path.join(directory, "{}.json".format(_safe_filename(strategy)))


def build_expected_return_artifact(
    strategy: str,
    samples: Iterable[Dict[str, object]],
    *,
    baseline_id: str,
    oos_result: Dict[str, object],
    top_k: int = 10,
    training_days: int = None,
    created_at: datetime = None,
) -> Dict[str, object]:
    training = build_training_samples(strategy, samples or [])
    window = _training_window(training, training_days=training_days)
    created = created_at or datetime.utcnow()
    max_age_days = _artifact_max_age_days()
    expires_at = created + timedelta(days=max_age_days) if max_age_days > 0 else None
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "strategy": str(strategy or ""),
        "created_at": _format_time(created),
        "expires_at": _format_time(expires_at) if expires_at else "",
        "baseline_id": str(baseline_id or ""),
        "training_window": window,
        "sample_count": len(training),
        "top_k": max(1, int(top_k or 10)),
        "model_confidence": _model_confidence(window["day_count"], len(training)),
        "oos_result": dict(oos_result or {}),
        "model_params": expected_return_model_params(),
    }


def save_expected_return_artifact(artifact: Dict[str, object], path: str = "", artifact_dir: str = "") -> str:
    target = path or expected_return_artifact_path(str((artifact or {}).get("strategy") or ""), artifact_dir=artifact_dir)
    atomic_write_json(target, artifact or {}, ensure_ascii=False, indent=2)
    return target


def load_expected_return_artifact(
    strategy: str,
    *,
    baseline_id: str = "",
    path: str = "",
    artifact_dir: str = "",
    now: datetime = None,
    max_age_days: int = None,
) -> Dict[str, object]:
    target = path or expected_return_artifact_path(strategy, artifact_dir=artifact_dir)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"ok": False, "status": "missing", "path": target}
    except Exception as exc:
        return {"ok": False, "status": "load_failed", "path": target, "error": str(exc)}
    if not isinstance(payload, dict):
        return {"ok": False, "status": "invalid_artifact", "path": target}
    validation = validate_expected_return_artifact(
        payload,
        strategy=strategy,
        baseline_id=baseline_id,
        now=now,
        max_age_days=max_age_days,
    )
    if not validation.get("ok"):
        return {**validation, "path": target, "artifact": payload}
    promotion = expected_return_artifact_promotion_gate(payload, baseline_id=baseline_id, now=now)
    return {"ok": True, "status": "loaded", "path": target, "artifact": payload, "promotion": promotion}


def validate_expected_return_artifact(
    artifact: Dict[str, object],
    *,
    strategy: str = "",
    baseline_id: str = "",
    now: datetime = None,
    max_age_days: int = None,
) -> Dict[str, object]:
    if not isinstance(artifact, dict):
        return {"ok": False, "status": "invalid_artifact"}
    if artifact.get("artifact_type") != ARTIFACT_TYPE:
        return {"ok": False, "status": "invalid_artifact_type"}
    if int(coerce_number(artifact.get("schema_version"), 0)) != ARTIFACT_SCHEMA_VERSION:
        return {"ok": False, "status": "schema_mismatch"}
    if strategy and str(artifact.get("strategy") or "") != str(strategy or ""):
        return {"ok": False, "status": "strategy_mismatch"}
    if baseline_id and str(artifact.get("baseline_id") or "") != str(baseline_id or ""):
        return {"ok": False, "status": "baseline_mismatch"}
    if _artifact_expired(artifact, now=now, max_age_days=max_age_days):
        return {"ok": False, "status": "expired"}
    return {"ok": True, "status": "valid"}


def expected_return_artifact_promotion_gate(
    artifact: Dict[str, object],
    *,
    baseline_id: str = "",
    now: datetime = None,
) -> Dict[str, object]:
    validation = validate_expected_return_artifact(
        artifact,
        strategy=str((artifact or {}).get("strategy") or ""),
        baseline_id=baseline_id,
        now=now,
    )
    if not validation.get("ok"):
        return {"can_promote": False, "status": validation.get("status", "invalid_artifact")}
    confidence = str((artifact or {}).get("model_confidence") or "")
    if confidence != "ready":
        return {"can_promote": False, "status": "model_not_ready", "model_confidence": confidence}
    oos_result = artifact.get("oos_result") if isinstance(artifact, dict) else {}
    oos_result = oos_result if isinstance(oos_result, dict) else {}
    oos_passed = _oos_gate_passed(oos_result)
    fdr_passed = _fdr_gate_passed(oos_result)
    ci_passed = _ci_gate_passed(oos_result)
    can_promote = oos_passed and fdr_passed and ci_passed
    if can_promote:
        status = "active"
    elif not oos_passed:
        status = "oos_blocked"
    elif not fdr_passed:
        status = "fdr_blocked"
    else:
        status = "ci_blocked"
    return {
        "can_promote": can_promote,
        "status": status,
        "model_confidence": confidence,
        "oos_passed": oos_passed,
        "fdr_passed": fdr_passed,
        "ci_passed": ci_passed,
    }


def expected_return_model_params() -> Dict[str, object]:
    return {
        "min_shadow_samples": MIN_SHADOW_SAMPLES,
        "min_ready_days": _min_ready_days(),
        "nearest_score_fraction": NEAREST_SCORE_FRACTION,
        "peer_selection": "nearest common strategy features",
        "feature_distance_min_fields": FEATURE_DISTANCE_MIN_FIELDS,
        "feature_fields": [key for key, _scale, _weight in FEATURE_SPECS],
        "ranking_field": "predicted_net_return",
        "time_decay_half_life_days": _time_decay_half_life_days(),
        "fallback_policy": "insufficient feature peers produce shadow diagnostics only",
    }


def build_training_samples(strategy: str, samples: Iterable[Dict[str, object]]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for sample in samples or []:
        raw = sample.get("raw") if isinstance(sample, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        primary_return_net = coerce_number(sample.get("primary_return_net"), None)
        if primary_return_net is None:
            continue
        features = _feature_values(raw, sample)
        if "score" not in features:
            stored_score = coerce_number(sample.get("stored_score"), None)
            if stored_score is not None:
                features["score"] = stored_score
        rows.append(
            {
                "strategy": strategy,
                "signal_date": str(sample.get("signal_date") or ""),
                **features,
                "primary_return_net": primary_return_net,
                "max_drawdown": coerce_number(sample.get("max_drawdown"), None),
            }
        )
    return rows


def predict_expected_return(
    strategy: str,
    rows: List[Dict[str, object]],
    samples: Iterable[Dict[str, object]] = None,
) -> List[Dict[str, object]]:
    training = build_training_samples(strategy, samples or [])
    day_count = len({row["signal_date"] for row in training if row.get("signal_date")})
    enriched = []
    for row in rows or []:
        item = dict(row)
        prediction = _predict_row(item, training, day_count)
        item.update(prediction)
        enriched.append(item)
    return enriched


def _predict_row(row: Dict[str, object], training: List[Dict[str, object]], day_count: int) -> Dict[str, object]:
    peers, peer_method = _nearest_training_samples(training, row)
    sample_count = len(peers)
    if sample_count < MIN_SHADOW_SAMPLES:
        return {
            "expected_return_net": None,
            "predicted_net_return": None,
            "p_win": None,
            "predicted_probability": None,
            "downside_p10": None,
            "expected_drawdown": None,
            "expected_return_uncertainty": None,
            "expected_return_time_decay_half_life": _time_decay_half_life_days(),
            "model_confidence": "low",
            "expected_return_sample_count": sample_count,
            "expected_return_peer_method": peer_method,
            "expected_return_available": False,
        }

    returns = [coerce_number(item.get("primary_return_net")) for item in peers]
    drawdowns = [coerce_number(item.get("max_drawdown"), 0.0) for item in peers]
    weights = _time_decay_weights(peers)
    expected = _weighted_avg(returns, weights)
    p_win = _weighted_avg([1.0 if value > 0 else 0.0 for value in returns], weights)
    downside_p10 = _weighted_quantile(returns, weights, 0.10)
    expected_drawdown = _weighted_avg(drawdowns, weights)
    uncertainty = _return_uncertainty(returns, weights)
    confidence = "ready" if day_count >= _min_ready_days() else "shadow"
    return {
        "expected_return_net": round(expected, 4),
        "predicted_net_return": round(expected, 4),
        "p_win": round(p_win, 4),
        "predicted_probability": round(p_win, 4),
        "downside_p10": round(downside_p10, 4),
        "expected_drawdown": round(expected_drawdown, 4),
        "expected_return_uncertainty": round(uncertainty, 4),
        "expected_return_time_decay_half_life": _time_decay_half_life_days(),
        "model_confidence": confidence,
        "expected_return_sample_count": sample_count,
        "expected_return_peer_method": peer_method,
        "expected_return_available": True,
    }


def _nearest_training_samples(training: List[Dict[str, object]], row: Dict[str, object]) -> tuple:
    peers = _nearest_feature_samples(training, row)
    if peers:
        return peers, "feature_nearest"
    return [], "insufficient_feature_peers"


def _nearest_feature_samples(training: List[Dict[str, object]], row: Dict[str, object]) -> List[Dict[str, object]]:
    if not training:
        return []
    row_features = _feature_values(row)
    ranked = []
    for item in training:
        distance = _feature_distance(row_features, item)
        if distance is None:
            continue
        ranked.append((distance, item))
    if len(ranked) < MIN_SHADOW_SAMPLES:
        return []
    ranked.sort(key=lambda pair: pair[0])
    limit = min(len(ranked), max(MIN_SHADOW_SAMPLES, int(len(ranked) * NEAREST_SCORE_FRACTION)))
    return [item for _distance, item in ranked[:limit]]


def _feature_values(*sources: Dict[str, object]) -> Dict[str, float]:
    values: Dict[str, float] = {}
    for key, _scale, _weight in FEATURE_SPECS:
        value = _first_number(key, sources)
        if value is not None:
            values[key] = value
    return values


def _first_number(key: str, sources: Iterable[Dict[str, object]]):
    for source in sources or []:
        if not isinstance(source, dict) or key not in source:
            continue
        value = coerce_number(source.get(key), None)
        if value is not None:
            return value
    return None


def _feature_distance(left: Dict[str, float], right: Dict[str, float]):
    total = 0.0
    weight_sum = 0.0
    field_count = 0
    for key, scale, weight in FEATURE_SPECS:
        if key not in left or key not in right:
            continue
        scale = max(1e-9, coerce_number(scale, 1.0))
        delta = (coerce_number(left.get(key)) - coerce_number(right.get(key))) / scale
        total += coerce_number(weight, 1.0) * delta * delta
        weight_sum += coerce_number(weight, 1.0)
        field_count += 1
    if field_count < FEATURE_DISTANCE_MIN_FIELDS or weight_sum <= 0:
        return None
    return total / weight_sum


def _time_decay_weights(rows: List[Dict[str, object]]) -> List[float]:
    dates = [_parse_signal_date(row.get("signal_date")) for row in rows or []]
    known_dates = [value for value in dates if value is not None]
    if not known_dates:
        return [1.0 for _row in rows or []]
    latest = max(known_dates)
    half_life = max(1, _time_decay_half_life_days())
    weights = []
    for value in dates:
        if value is None:
            weights.append(1.0)
            continue
        age_days = max(0, (latest - value).days)
        weights.append(0.5 ** (age_days / half_life))
    return weights


def _weighted_avg(values: List[float], weights: List[float]) -> float:
    pairs = [
        (coerce_number(value), max(0.0, coerce_number(weight, 0.0)))
        for value, weight in zip(values or [], weights or [])
    ]
    total_weight = sum(weight for _value, weight in pairs)
    if total_weight <= 0:
        return _avg([value for value, _weight in pairs])
    return sum(value * weight for value, weight in pairs) / total_weight


def _weighted_quantile(values: List[float], weights: List[float], q: float) -> float:
    pairs = sorted(
        (
            (coerce_number(value), max(0.0, coerce_number(weight, 0.0)))
            for value, weight in zip(values or [], weights or [])
        ),
        key=lambda item: item[0],
    )
    total_weight = sum(weight for _value, weight in pairs)
    if not pairs:
        return 0.0
    if total_weight <= 0:
        return _quantile([value for value, _weight in pairs], q)
    target = max(0.0, min(1.0, coerce_number(q, 0.0))) * total_weight
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= target:
            return value
    return pairs[-1][0]


def _return_uncertainty(values: List[float], weights: List[float]) -> float:
    if not values:
        return 0.0
    mean = _weighted_avg(values, weights)
    total_weight = sum(max(0.0, coerce_number(weight, 0.0)) for weight in weights or [])
    if total_weight <= 0:
        total_weight = len(values)
        variance = sum((coerce_number(value) - mean) ** 2 for value in values) / max(1, len(values))
    else:
        variance = sum(
            max(0.0, coerce_number(weight, 0.0)) * (coerce_number(value) - mean) ** 2
            for value, weight in zip(values, weights)
        ) / total_weight
    effective_n = max(1.0, total_weight)
    return math.sqrt(max(0.0, variance)) / math.sqrt(effective_n)


def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * q))))
    return ordered[index]


def _training_window(training: List[Dict[str, object]], training_days: int = None) -> Dict[str, object]:
    dates = sorted({str(row.get("signal_date") or "") for row in training if row.get("signal_date")})
    return {
        "start": dates[0] if dates else "",
        "end": dates[-1] if dates else "",
        "day_count": len(dates),
        "sample_count": len(training),
        "requested_days": int(training_days or 0),
    }


def _model_confidence(day_count: int, sample_count: int) -> str:
    if sample_count < MIN_SHADOW_SAMPLES:
        return "low"
    return "ready" if int(day_count or 0) >= _min_ready_days() else "shadow"


def _min_ready_days() -> int:
    return int(coerce_number(getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", MIN_READY_DAYS), MIN_READY_DAYS))


def _time_decay_half_life_days() -> int:
    return max(1, int(coerce_number(getattr(config, "CALIBRATE_TIME_DECAY_HALF_LIFE", 60), 60)))


def _artifact_max_age_days() -> int:
    return int(coerce_number(getattr(config, "EXPECTED_RETURN_ARTIFACT_MAX_AGE_DAYS", 7), 7))


def _artifact_expired(artifact: Dict[str, object], *, now: datetime = None, max_age_days: int = None) -> bool:
    current = now or datetime.utcnow()
    expires_at = _parse_time(str((artifact or {}).get("expires_at") or ""))
    if expires_at is not None:
        return current > expires_at
    created_at = _parse_time(str((artifact or {}).get("created_at") or ""))
    age_days = _artifact_max_age_days() if max_age_days is None else int(max_age_days)
    if created_at is None or age_days <= 0:
        return False
    return current - created_at > timedelta(days=age_days)


def _oos_gate_passed(oos_result: Dict[str, object]) -> bool:
    baseline = coerce_number(oos_result.get("baseline_oos_objective"), None)
    predicted_return = coerce_number(oos_result.get("predicted_net_return_oos_objective"), None)
    if predicted_return is None:
        predicted_return = coerce_number(oos_result.get("rank_score_oos_objective"), None)
    margin = coerce_number(oos_result.get("margin"), coerce_number(getattr(config, "CALIBRATE_IMPROVE_MARGIN", 0.05), 0.05))
    positive_folds = int(coerce_number(oos_result.get("positive_folds"), 0))
    fold_count = int(coerce_number(oos_result.get("fold_count"), 0))
    objective_passed = baseline is not None and predicted_return is not None and predicted_return > baseline + margin
    folds_passed = fold_count > 0 and positive_folds > fold_count // 2
    status_passed = str(oos_result.get("status") or "") == "oos_passed" or bool(oos_result.get("oos_passed"))
    return bool(oos_result.get("ok")) and status_passed and objective_passed and folds_passed


def _fdr_gate_passed(oos_result: Dict[str, object]) -> bool:
    fdr = oos_result.get("fdr") if isinstance(oos_result, dict) else {}
    return isinstance(fdr, dict) and bool(fdr.get("passed"))


def _ci_gate_passed(oos_result: Dict[str, object]) -> bool:
    ci = oos_result.get("ci") if isinstance(oos_result, dict) else {}
    if isinstance(ci, dict) and "passed" in ci:
        return bool(ci.get("passed"))
    low = coerce_number(oos_result.get("avg_return_improvement_ci95_low"), None)
    return low is not None and low >= 0.0


def _safe_filename(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return text or "default"


def _format_time(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return value.replace(microsecond=0).isoformat()


def _parse_time(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _parse_signal_date(value) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    compact = text[:10].replace("-", "")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(compact if fmt == "%Y%m%d" else text[:10], fmt)
        except Exception:
            continue
    return None
