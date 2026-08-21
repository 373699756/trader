"""Append-only artifact storage for risk-adjusted daily trend research."""

from __future__ import annotations

import json
import os
from pathlib import Path

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.score_r6_daily_models import ScoreR6DailyReport
from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC


class ScoreR6DailyArtifactConflictError(RuntimeError):
    pass


class ScoreR6DailyArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(self, report: ScoreR6DailyReport) -> str:
        if report.research_identity != SCORE_R6_DAILY_SPEC.research_identity:
            raise ValueError("daily trend report identity is invalid")
        if report.research_spec_hash != SCORE_R6_DAILY_SPEC.content_hash:
            raise ValueError("daily trend report spec binding is invalid")
        if report.status == "insufficient_coverage":
            raise ValueError("daily trend incomplete coverage cannot be sealed")
        path = self._report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stored_hash = self._verify(path)
            if stored_hash != report.content_hash:
                raise ScoreR6DailyArtifactConflictError("daily trend artifact identity conflict")
            return stored_hash
        payload = canonical_value(report)
        if not isinstance(payload, dict):
            raise TypeError("daily trend artifact must be a JSON object")
        payload["content_hash"] = report.content_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                stored_hash = self._verify(path)
                if stored_hash != report.content_hash:
                    raise ScoreR6DailyArtifactConflictError("daily trend artifact identity conflict") from None
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
                "historical_gate_passed": False,
                "selected_candidate_hash": "",
                "failure_reasons": [],
                "promotion_authority": False,
            }
        candidate = payload.get("selected_candidate")
        candidate_hash = canonical_hash(candidate) if isinstance(candidate, dict) else ""
        return {
            "report_hash": payload["content_hash"],
            "status": payload.get("status", "historical_rejected"),
            "historical_gate_passed": bool(payload.get("historical_gate_passed", False)),
            "selected_candidate_hash": candidate_hash,
            "failure_reasons": payload.get("failure_reasons", []),
            "promotion_authority": False,
        }

    def _report_path(self) -> Path:
        return self._root / SCORE_R6_DAILY_SPEC.research_identity / "historical-report.json"

    @staticmethod
    def _verify(path: Path) -> str:
        return str(ScoreR6DailyArtifactStore._read_verified(path)["content_hash"])

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
            raise ScoreR6DailyArtifactConflictError("daily trend artifact hash or schema is invalid") from exc
        raw["content_hash"] = stored_hash
        return raw


__all__ = ["ScoreR6DailyArtifactConflictError", "ScoreR6DailyArtifactStore"]
