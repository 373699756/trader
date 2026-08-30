"""Immutable report and model storage for Tomorrow P2 historical screening."""

from __future__ import annotations

import json
import os
from pathlib import Path

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.tomorrow_historical_p2_models import TomorrowHistoricalP2Report
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2ModelArtifact
from trader.domain.research.tomorrow_historical_p2 import TOMORROW_HISTORICAL_P2_SPEC


class TomorrowHistoricalP2ArtifactConflictError(RuntimeError):
    pass


class TomorrowHistoricalP2ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(
        self,
        report: TomorrowHistoricalP2Report,
        model_artifact: TomorrowHistoricalP2ModelArtifact | None,
    ) -> str:
        if report.research_spec_hash != TOMORROW_HISTORICAL_P2_SPEC.content_hash:
            raise ValueError("Tomorrow P2 report spec binding is invalid")
        expected_model_hash = model_artifact.content_hash if model_artifact is not None else None
        if report.model_artifact_hash != expected_model_hash:
            raise ValueError("Tomorrow P2 report model binding is invalid")
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
                raise TomorrowHistoricalP2ArtifactConflictError("Tomorrow P2 artifact model binding is invalid")
        return report

    def inspect(self) -> dict[str, object]:
        report = self.read_report_payload()
        if report is None:
            return {
                "report_hash": "",
                "status": "not_run",
                "candidate_id": TOMORROW_HISTORICAL_P2_SPEC.candidate.candidate_id,
                "failure_reasons": [],
                "forward_preregistration_eligible": False,
                "production_authority": False,
            }
        return {
            "report_hash": report["content_hash"],
            "status": report.get("status", "historical_rejected"),
            "candidate_id": report.get("candidate_id", ""),
            "failure_reasons": report.get("failure_reasons", []),
            "forward_preregistration_eligible": report.get("status") == "historical_passed",
            "production_authority": False,
        }

    def _seal_value(self, path: Path, value: object, content_hash: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stored_hash = str(self._read_verified(path, path.stem)["content_hash"])
            if stored_hash != content_hash:
                raise TomorrowHistoricalP2ArtifactConflictError("Tomorrow P2 artifact identity conflict")
            return stored_hash
        payload = canonical_value(value)
        if not isinstance(payload, dict):
            raise TypeError("Tomorrow P2 artifact must be a JSON object")
        payload["content_hash"] = content_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                stored_hash = str(self._read_verified(path, path.stem)["content_hash"])
                if stored_hash != content_hash:
                    raise TomorrowHistoricalP2ArtifactConflictError("Tomorrow P2 artifact identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        return str(self._read_verified(path, path.stem)["content_hash"])

    def _report_path(self) -> Path:
        return self._root / TOMORROW_HISTORICAL_P2_SPEC.research_identity / "historical-report.json"

    def _model_path(self) -> Path:
        return self._root / TOMORROW_HISTORICAL_P2_SPEC.research_identity / "model-artifact.json"

    @staticmethod
    def _read_verified(path: Path, label: str) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("artifact payload is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("artifact hash mismatch")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowHistoricalP2ArtifactConflictError(
                f"Tomorrow P2 {label} artifact hash or schema is invalid"
            ) from exc
        raw["content_hash"] = stored_hash
        return raw


__all__ = ["TomorrowHistoricalP2ArtifactConflictError", "TomorrowHistoricalP2ArtifactStore"]
