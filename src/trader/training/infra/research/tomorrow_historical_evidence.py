"""Immutable publication for Tomorrow historical evidence."""

from __future__ import annotations

import json
from pathlib import Path

from trader.infra.serialization.sealing import PublishImmutable
from trader.training.application.tomorrow_historical_report import TomorrowHistoricalReport
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash
from trader.training.domain.evaluation.tomorrow_historical import (
    TOMORROW_HISTORICAL_SPEC,
    TomorrowHistoricalModelSnapshot,
)
from trader.infra.serialization.canonical_codec import CanonicalText, EncodeCanonicalValue


class TomorrowHistoricalEvidenceConflictError(RuntimeError):
    pass


class TomorrowHistoricalEvidenceIndex:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(
        self,
        report: TomorrowHistoricalReport,
        model_artifact: TomorrowHistoricalModelSnapshot | None,
    ) -> str:
        if report.research_spec_hash != TOMORROW_HISTORICAL_SPEC.content_hash:
            raise ValueError("Tomorrow historical report spec binding is invalid")
        expected_model_hash = model_artifact.content_hash if model_artifact is not None else None
        if report.model_artifact_hash != expected_model_hash:
            raise ValueError("Tomorrow historical report model binding is invalid")
        if model_artifact is not None:
            self._seal_value(self._model_path(), model_artifact, model_artifact.content_hash)
        return self._seal_value(self._report_path(), report, report.content_hash)

    def read_report_payload(self) -> dict[str, object] | None:
        path = self._report_path()
        if not path.is_file():
            return None
        report = self._read_verified(path, "report")
        model_hash = report.get("model_artifact_hash")
        if model_hash is not None:
            model = self._read_verified(self._model_path(), "model")
            if model.get("content_hash") != model_hash:
                raise TomorrowHistoricalEvidenceConflictError("Tomorrow historical artifact model binding is invalid")
        return report

    def inspect(self) -> dict[str, object]:
        report = self.read_report_payload()
        if report is None:
            return {
                "report_hash": "",
                "status": "not_run",
                "candidate_id": TOMORROW_HISTORICAL_SPEC.candidate.candidate_id,
                "failure_reasons": [],
                "validation_mode": "historical_only",
                "production_authority": False,
            }
        return {
            "report_hash": report["content_hash"],
            "status": report.get("status", "historical_rejected"),
            "candidate_id": report.get("candidate_id", ""),
            "failure_reasons": report.get("failure_reasons", []),
            "validation_mode": "historical_only",
            "production_authority": False,
        }

    def _seal_value(self, path: Path, value: object, content_hash: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            persisted_hash = str(self._read_verified(path, path.stem)["content_hash"])
            if persisted_hash != content_hash:
                raise TomorrowHistoricalEvidenceConflictError("Tomorrow historical artifact identity conflict")
            return persisted_hash
        payload = EncodeCanonicalValue(value)
        if not isinstance(payload, dict):
            raise TypeError("Tomorrow historical artifact must be a JSON object")
        payload["content_hash"] = content_hash
        if PublishImmutable(path, CanonicalText(payload)):
            return str(content_hash)
        persisted_hash = str(self._read_verified(path, path.stem)["content_hash"])
        if persisted_hash != content_hash:
            raise TomorrowHistoricalEvidenceConflictError("Tomorrow historical artifact identity conflict")
        return persisted_hash

    def _report_path(self) -> Path:
        return self._root / TOMORROW_HISTORICAL_SPEC.research_identity / "historical-report.json"

    def _model_path(self) -> Path:
        return self._root / TOMORROW_HISTORICAL_SPEC.research_identity / "model-artifact.json"

    @staticmethod
    def _read_verified(path: Path, label: str) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("artifact payload is not an object")
            expected = (
                {'candidate_id', 'feature_ids', 'transformer_means', 'transformer_scales', 'linear_intercept', 'linear_coefficients', 'lightgbm_model', 'lightgbm_best_iteration', 'training_rows', 'internal_validation_rows'}
                if path.name == "model-artifact.json" else
                {'research_spec_hash', 'source_spec_hash', 'source_manifest_hash', 'source_universe_hash', 'source_histories_hash', 'candidate_id', 'status', 'metrics', 'training_evidence_hash', 'validation_evidence_hash', 'model_artifact_hash', 'failure_reasons', 'production_authority'}
            )
            if set(raw) != expected | {"content_hash"}:
                raise ValueError("artifact fields are invalid")
            persisted_hash = raw.pop("content_hash")
            if not isinstance(persisted_hash, str) or CanonicalResearchHash(raw) != persisted_hash:
                raise ValueError("artifact hash mismatch")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowHistoricalEvidenceConflictError(
                f"Tomorrow historical {label} artifact hash or schema is invalid"
            ) from exc
        raw["content_hash"] = persisted_hash
        return raw


__all__ = ["TomorrowHistoricalEvidenceConflictError", "TomorrowHistoricalEvidenceIndex"]
