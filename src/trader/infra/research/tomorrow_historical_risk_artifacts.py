"""Tamper-evident storage for historical Tomorrow risk validation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.tomorrow_historical_validation import (
    HISTORICAL_RISK_VALIDATION_SPEC,
    HistoricalRiskValidationOutcome,
)


class TomorrowHistoricalRiskArtifactConflictError(RuntimeError):
    pass


class TomorrowHistoricalRiskArtifactStore:
    def __init__(self, runtime_dir: Path) -> None:
        self._root = runtime_dir / "tomorrow-v2-historical-risk" / HISTORICAL_RISK_VALIDATION_SPEC.research_identity

    def seal(self, outcome: HistoricalRiskValidationOutcome) -> str:
        report = outcome.report
        model = outcome.model_artifact
        if report.status == "historical_data_insufficient" or model is None:
            raise ValueError("insufficient historical risk evidence is not a terminal artifact")
        self._write(self._model_path(), model, model.content_hash)
        self._write(self._report_path(), report, report.content_hash)
        return report.content_hash

    def read_report_payload(self) -> dict[str, object] | None:
        path = self._report_path()
        if not path.is_file():
            return None
        report = self._read_verified(path)
        model = self._read_verified(self._model_path())
        if report.get("model_artifact_hash") != model.get("content_hash"):
            raise TomorrowHistoricalRiskArtifactConflictError("historical risk model binding is invalid")
        return report

    def inspect(self) -> dict[str, object]:
        report = self.read_report_payload()
        if report is None:
            return {
                "status": "not_run",
                "report_hash": "",
                "model_artifact_hash": "",
                "production_authority": False,
            }
        return {
            "status": report.get("status", "artifact_invalid"),
            "report_hash": report.get("content_hash", ""),
            "model_artifact_hash": report.get("model_artifact_hash", ""),
            "brier_score": report.get("brier_score"),
            "baseline_brier_score": report.get("baseline_brier_score"),
            "expected_calibration_error": report.get("expected_calibration_error"),
            "production_authority": False,
        }

    def _write(self, path: Path, value: object, expected_hash: str) -> None:
        payload = canonical_value(value)
        if not isinstance(payload, dict):
            raise TypeError("historical risk artifact must serialize to an object")
        payload["content_hash"] = expected_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self._read_verified(path).get("content_hash") != expected_hash:
                raise TomorrowHistoricalRiskArtifactConflictError("historical risk artifact identity conflict")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self._read_verified(path).get("content_hash") != expected_hash:
                    raise TomorrowHistoricalRiskArtifactConflictError(
                        "historical risk artifact identity conflict"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read_verified(path: Path) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("historical risk artifact is not an object")
            stored = raw.pop("content_hash")
            if not isinstance(stored, str) or canonical_hash(raw) != stored:
                raise ValueError("historical risk artifact hash mismatch")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowHistoricalRiskArtifactConflictError("historical risk artifact is invalid") from exc
        raw["content_hash"] = stored
        return raw

    def _model_path(self) -> Path:
        return self._root / "model-artifact.json"

    def _report_path(self) -> Path:
        return self._root / "validation-report.json"


__all__ = ["TomorrowHistoricalRiskArtifactConflictError", "TomorrowHistoricalRiskArtifactStore"]
