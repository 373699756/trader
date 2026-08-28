"""Append-only artifact boundary for complete Tomorrow shadow reports."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from trader.application.research.shadow_model_models import ShadowFoldRecord, ShadowModelReport, ShadowPrediction


class ShadowModelArtifactConflictError(RuntimeError):
    pass


class ShadowModelArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(self, report: ShadowModelReport) -> str:
        window = f"{report.training_window_start.isoformat()}_{report.training_window_end.isoformat()}"
        path = self._root / report.spec_hash / window / "shadow-report.json"
        expected = _report_payload(report)
        if _canonical_hash(expected) != report.content_hash:
            raise ShadowModelArtifactConflictError("shadow report hash or schema is invalid")
        if path.exists():
            _verify(path, report.content_hash, expected)
            return report.content_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**expected, "content_hash": report.content_hash}
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(_canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                _verify(path, report.content_hash, expected)
        finally:
            temporary.unlink(missing_ok=True)
        _verify(path, report.content_hash, expected)
        return report.content_hash


def _verify(path: Path, expected_hash: str, expected_payload: dict[str, object]) -> None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("shadow artifact payload is not an object")
        stored_hash = raw.pop("content_hash")
        if not isinstance(stored_hash, str) or stored_hash != expected_hash:
            raise ValueError("shadow artifact identity mismatch")
        if _canonical_hash(raw) != stored_hash or raw != json.loads(_canonical_json(expected_payload)):
            raise ValueError("shadow artifact payload mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ShadowModelArtifactConflictError("shadow report hash or schema is invalid") from exc


def _report_payload(report: ShadowModelReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "spec_hash": report.spec_hash,
        "feature_version": report.feature_version,
        "training_window_start": report.training_window_start.isoformat(),
        "training_window_end": report.training_window_end.isoformat(),
        "random_seed": report.random_seed,
        "cost_bps": report.cost_bps,
        "status": report.status,
        "production_authority": report.production_authority,
        "folds": tuple(_fold_payload(item) for item in report.folds),
        "predictions": tuple(_prediction_payload(item) for item in report.predictions),
    }


def _fold_payload(item: ShadowFoldRecord) -> dict[str, object]:
    return {
        "horizon": item.horizon,
        "window_mode": item.window_mode,
        "prediction_date": item.prediction_date.isoformat(),
        "model_family": item.model_family,
        "train_start": item.train_start.isoformat(),
        "train_end": item.train_end.isoformat(),
        "validation_start": item.validation_start.isoformat(),
        "validation_end": item.validation_end.isoformat(),
        "calibration_start": item.calibration_start.isoformat(),
        "calibration_end": item.calibration_end.isoformat(),
        "train_date_count": item.train_date_count,
        "net_model_hash": item.net_model_hash,
        "severe_model_hash": item.severe_model_hash,
    }


def _prediction_payload(item: ShadowPrediction) -> dict[str, object]:
    return {
        "prediction_date": item.prediction_date.isoformat(),
        "code": item.code,
        "board": item.board,
        "industry": item.industry,
        "horizon": item.horizon,
        "window_mode": item.window_mode,
        "feature_batch_hash": item.feature_batch_hash,
        "estimated_cost": item.estimated_cost,
        "actual_net_excess": item.actual_net_excess,
        "actual_severe_loss": item.actual_severe_loss,
        "linear_net_excess": item.linear_net_excess,
        "lightgbm_net_excess": item.lightgbm_net_excess,
        "linear_severe_probability": item.linear_severe_probability,
        "lightgbm_severe_probability": item.lightgbm_severe_probability,
        "uncertainty": item.uncertainty,
    }


def _canonical_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


__all__ = ["ShadowModelArtifactConflictError", "ShadowModelArtifactStore"]
