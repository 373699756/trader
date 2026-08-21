"""Append-only artifact storage for daily ranking stability research."""

from __future__ import annotations

import json
import os
from pathlib import Path

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.score_r6_stability_models import ScoreR6StabilityReport
from trader.domain.research.score_r6_stability import SCORE_R6_STABILITY_SPEC


class ScoreR6StabilityArtifactConflictError(RuntimeError):
    pass


class ScoreR6StabilityArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(self, report: ScoreR6StabilityReport) -> str:
        if report.research_identity != SCORE_R6_STABILITY_SPEC.research_identity:
            raise ValueError("daily stability report identity is invalid")
        if report.research_spec_hash != SCORE_R6_STABILITY_SPEC.content_hash:
            raise ValueError("daily stability report spec binding is invalid")
        if (
            report.parent_report_hash != SCORE_R6_STABILITY_SPEC.parent_report_hash
            or report.parent_candidate_hash != SCORE_R6_STABILITY_SPEC.parent_candidate_hash
        ):
            raise ValueError("daily stability report parent binding is invalid")
        if report.status in {"insufficient_coverage", "parent_mismatch"}:
            raise ValueError("daily stability prerequisite failure cannot be sealed")
        path = self._report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stored_hash = self._verify(path)
            if stored_hash != report.content_hash:
                raise ScoreR6StabilityArtifactConflictError("daily stability artifact identity conflict")
            return stored_hash
        payload = canonical_value(report)
        if not isinstance(payload, dict):
            raise TypeError("daily stability artifact must be a JSON object")
        payload["content_hash"] = report.content_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                stored_hash = self._verify(path)
                if stored_hash != report.content_hash:
                    raise ScoreR6StabilityArtifactConflictError("daily stability artifact identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        return self._verify(path)

    def read_payload(self) -> dict[str, object] | None:
        path = self._report_path()
        return self._read_verified(path) if path.is_file() else None

    def inspect(self) -> dict[str, object]:
        payload = self.read_payload()
        if payload is None:
            return {
                "report_hash": "",
                "status": "not_run",
                "diagnostic_gate_passed": False,
                "selected_candidate_hash": "",
                "failure_reasons": [],
                "evidence_class": SCORE_R6_STABILITY_SPEC.evidence_class,
                "promotion_authority": False,
            }
        candidate = payload.get("selected_candidate")
        candidate_hash = canonical_hash(candidate) if isinstance(candidate, dict) else ""
        return {
            "report_hash": payload["content_hash"],
            "status": payload.get("status", "historical_rejected"),
            "diagnostic_gate_passed": bool(payload.get("diagnostic_gate_passed", False)),
            "selected_candidate_hash": candidate_hash,
            "failure_reasons": payload.get("failure_reasons", []),
            "evidence_class": payload.get("evidence_class", SCORE_R6_STABILITY_SPEC.evidence_class),
            "promotion_authority": False,
        }

    def _report_path(self) -> Path:
        return self._root / SCORE_R6_STABILITY_SPEC.research_identity / "diagnostic-report.json"

    @staticmethod
    def _verify(path: Path) -> str:
        return str(ScoreR6StabilityArtifactStore._read_verified(path)["content_hash"])

    @staticmethod
    def _read_verified(path: Path) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("artifact payload is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("artifact hash mismatch")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ScoreR6StabilityArtifactConflictError("daily stability artifact hash or schema is invalid") from exc
        raw["content_hash"] = stored_hash
        return raw


__all__ = ["ScoreR6StabilityArtifactConflictError", "ScoreR6StabilityArtifactStore"]
