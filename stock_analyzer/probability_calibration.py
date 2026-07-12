from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from . import config
from .normalization import coerce_number
from .runtime_json import atomic_write_json


def _calibrator_path(strategy: str, directory: str = "") -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(strategy or "strategy"))
    return os.path.join(directory or getattr(config, "SCORE_CALIBRATOR_DIR", ".runtime"), f"score_calibrator_{safe}.json")


class ScoreCalibrator:
    def __init__(self, buckets: List[Dict[str, object]] = None, sample_count: int = 0, strategy: str = ""):
        self.buckets = buckets or []
        self.sample_count = int(sample_count or 0)
        self.strategy = strategy

    @property
    def is_fitted(self) -> bool:
        return bool(self.buckets)

    def predict(self, score) -> Optional[Dict[str, object]]:
        if not self.buckets:
            return None
        value = coerce_number(score, None)
        if value is None:
            return None
        bucket = self._bucket_for_score(value)
        if not bucket:
            return None
        probability = coerce_number(bucket.get("probability"), None)
        if probability is None:
            return None
        return {
            "calibrated_probability": round(probability, 4),
            "probability_label": probability_label(probability),
            "probability_sample_count": int(bucket.get("sample_count") or 0),
            "probability_bucket": bucket.get("label") or "",
            "probability_avg_return": round(coerce_number(bucket.get("avg_return")), 4),
            "probability_role": "diagnostic_only",
            "probability_trading_enabled": False,
        }

    def _bucket_for_score(self, score: float) -> Optional[Dict[str, object]]:
        for bucket in self.buckets:
            if coerce_number(bucket.get("min_score"), score) <= score <= coerce_number(bucket.get("max_score"), score):
                return bucket
        candidates = [bucket for bucket in self.buckets if score <= coerce_number(bucket.get("max_score"), score)]
        if candidates:
            return candidates[0]
        return self.buckets[-1] if self.buckets else None

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": 1,
            "strategy": self.strategy,
            "sample_count": self.sample_count,
            "buckets": self.buckets,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "ScoreCalibrator":
        buckets = payload.get("buckets") if isinstance(payload, dict) else []
        if not isinstance(buckets, list):
            buckets = []
        return cls(
            buckets=[bucket for bucket in buckets if isinstance(bucket, dict)],
            sample_count=coerce_number(payload.get("sample_count") if isinstance(payload, dict) else 0),
            strategy=str(payload.get("strategy") or "") if isinstance(payload, dict) else "",
        )


def train_score_calibrator(
    strategy: str,
    samples: Iterable[Dict[str, object]],
    min_samples: int = None,
    bucket_count: int = None,
) -> ScoreCalibrator:
    rows = _training_rows(samples)
    min_samples = int(min_samples or getattr(config, "SCORE_CALIBRATION_MIN_SAMPLES", 20))
    bucket_count = max(2, int(bucket_count or getattr(config, "SCORE_CALIBRATION_BUCKETS", 5)))
    if len(rows) < min_samples:
        return ScoreCalibrator(strategy=strategy)
    rows.sort(key=lambda item: item["score"])
    buckets = _build_monotonic_buckets(rows, bucket_count)
    return ScoreCalibrator(buckets=buckets, sample_count=len(rows), strategy=strategy)


def apply_score_calibration(rows: List[Dict[str, object]], calibrator: ScoreCalibrator) -> None:
    if not rows or not calibrator or not calibrator.is_fitted:
        return
    for row in rows:
        score = row.get("decision_score", row.get("score"))
        prediction = calibrator.predict(score)
        if not prediction:
            continue
        row.update(prediction)
        row["score_note"] = _score_note(row, prediction)


def save_calibrator(calibrator: ScoreCalibrator, path: str = "") -> str:
    target = path or _calibrator_path(calibrator.strategy)
    atomic_write_json(target, calibrator.to_dict(), ensure_ascii=False, indent=2)
    return target


def load_calibrator(strategy: str, path: str = "") -> ScoreCalibrator:
    target = path or _calibrator_path(strategy)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return ScoreCalibrator(strategy=strategy)
    calibrator = ScoreCalibrator.from_dict(payload if isinstance(payload, dict) else {})
    calibrator.strategy = calibrator.strategy or strategy
    return calibrator


def build_and_save_calibrator(
    strategy: str,
    samples: Iterable[Dict[str, object]],
    path: str = "",
    min_samples: int = None,
    bucket_count: int = None,
) -> Dict[str, object]:
    calibrator = train_score_calibrator(strategy, samples, min_samples=min_samples, bucket_count=bucket_count)
    if not calibrator.is_fitted:
        return {
            "ok": True,
            "strategy": strategy,
            "status": "insufficient_samples",
            "sample_count": calibrator.sample_count,
            "min_samples": int(min_samples or getattr(config, "SCORE_CALIBRATION_MIN_SAMPLES", 20)),
        }
    target = save_calibrator(calibrator, path=path)
    return {
        "ok": True,
        "strategy": strategy,
        "status": "written",
        "sample_count": calibrator.sample_count,
        "bucket_count": len(calibrator.buckets),
        "path": target,
    }


def probability_label(probability: float) -> str:
    value = coerce_number(probability)
    if value >= 0.65:
        return "高置信"
    if value >= 0.55:
        return "中等置信"
    if value >= 0.48:
        return "接近随机"
    return "低置信"


def _training_rows(samples: Iterable[Dict[str, object]]) -> List[Dict[str, float]]:
    rows: List[Dict[str, float]] = []
    for sample in samples or []:
        raw = sample.get("raw") if isinstance(sample, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        score = coerce_number(raw.get("decision_score", raw.get("score", sample.get("stored_score"))), None)
        ret = coerce_number(sample.get("primary_return_net") if isinstance(sample, dict) else None, None)
        if score is None or ret is None:
            continue
        rows.append({"score": score, "return": ret, "win": 1.0 if ret > 0 else 0.0})
    return rows


def _build_monotonic_buckets(rows: List[Dict[str, float]], bucket_count: int) -> List[Dict[str, object]]:
    bucket_size = max(1, int(round(len(rows) / max(1, bucket_count))))
    raw_buckets: List[Dict[str, object]] = []
    index = 0
    while index < len(rows):
        end = min(len(rows), index + bucket_size)
        while end < len(rows) and rows[end]["score"] == rows[end - 1]["score"]:
            end += 1
        chunk = rows[index:end]
        index = end
        if not chunk:
            continue
        sample_count = len(chunk)
        probability = sum(item["win"] for item in chunk) / sample_count
        avg_return = sum(item["return"] for item in chunk) / sample_count
        min_score = chunk[0]["score"]
        max_score = chunk[-1]["score"]
        raw_buckets.append(
            {
                "min_score": round(min_score, 4),
                "max_score": round(max_score, 4),
                "label": _bucket_label(min_score, max_score),
                "sample_count": sample_count,
                "probability": probability,
                "win_rate": probability * 100.0,
                "avg_return": avg_return,
            }
        )
    probabilities = _monotonic_probabilities(
        [coerce_number(bucket["probability"]) for bucket in raw_buckets],
        [int(bucket["sample_count"] or 0) for bucket in raw_buckets],
    )
    for bucket, probability in zip(raw_buckets, probabilities):
        bucket["probability"] = round(max(0.01, min(0.99, probability)), 4)
        bucket["win_rate"] = round(bucket["probability"] * 100.0, 2)
        bucket["avg_return"] = round(coerce_number(bucket["avg_return"]), 4)
    return raw_buckets


def _monotonic_probabilities(values: List[float], weights: List[int] = None) -> List[float]:
    if not values:
        return []
    weights = weights or [1] * len(values)
    blocks = [
        {
            "count": 1,
            "weight": max(1, int(weight or 1)),
            "value": coerce_number(value),
        }
        for value, weight in zip(values, weights)
    ]
    index = 0
    while index < len(blocks) - 1:
        if blocks[index]["value"] <= blocks[index + 1]["value"] + 1e-12:
            index += 1
            continue
        total_weight = blocks[index]["weight"] + blocks[index + 1]["weight"]
        total_count = blocks[index]["count"] + blocks[index + 1]["count"]
        avg = (
            blocks[index]["value"] * blocks[index]["weight"]
            + blocks[index + 1]["value"] * blocks[index + 1]["weight"]
        ) / total_weight
        blocks[index : index + 2] = [{"count": total_count, "weight": total_weight, "value": avg}]
        index = max(0, index - 1)
    result: List[float] = []
    for block in blocks:
        result.extend([block["value"]] * int(block["count"]))
    return result


def _bucket_label(low: float, high: float) -> str:
    if abs(high - low) < 1e-9:
        return f"{low:.0f}"
    return f"{low:.0f}-{high:.0f}"


def _score_note(row: Dict[str, object], prediction: Dict[str, object]) -> str:
    score = coerce_number(row.get("decision_score", row.get("score")))
    probability = coerce_number(prediction.get("calibrated_probability")) * 100.0
    sample_count = int(prediction.get("probability_sample_count") or 0)
    return "综合分 {:.1f}，历史同类信号诊断胜率 {:.1f}%（{}样本，仅供校准观察）".format(
        score,
        probability,
        sample_count,
    )
