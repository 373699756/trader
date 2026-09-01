"""Tamper-evident storage for the historical-only Score-R6 report."""

from __future__ import annotations

import json
import os
from pathlib import Path

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.score_r6_models import ScoreR6HistoricalReport
from trader.domain.research.score_r6 import SCORE_R6_HISTORICAL_SPEC


class ScoreR6ArtifactConflictError(RuntimeError):
    pass


class ScoreR6ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal_historical(self, report: ScoreR6HistoricalReport) -> str:
        path = self._root / report.research_identity / "historical-report.json"
        payload = canonical_value(report)
        if not isinstance(payload, dict):
            raise TypeError("Score-R6 historical report must serialize to an object")
        payload["content_hash"] = report.content_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = self._read_verified(path)
            if existing.get("content_hash") != report.content_hash:
                raise ScoreR6ArtifactConflictError("Score-R6 historical report identity conflict")
            return report.content_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self._read_verified(path)
                if existing.get("content_hash") != report.content_hash:
                    raise ScoreR6ArtifactConflictError("Score-R6 historical report identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        return report.content_hash

    def read_historical_payload(self) -> dict[str, object] | None:
        path = self._root / SCORE_R6_HISTORICAL_SPEC.research_identity / "historical-report.json"
        return self._read_verified(path) if path.is_file() else None

    def inspect(self) -> dict[str, object]:
        historical = self.read_historical_payload()
        return {
            "historical_report_hash": historical.get("content_hash", "") if historical is not None else "",
            "historical_gate_passed": bool(historical.get("historical_gate_passed", False))
            if historical is not None
            else False,
            "validation_mode": "historical_only",
            "production_authority": False,
        }

    @staticmethod
    def _read_verified(path: Path) -> dict[str, object]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("Score-R6 artifact is not an object")
            stored = raw.pop("content_hash")
            if not isinstance(stored, str) or canonical_hash(raw) != stored:
                raise ValueError("Score-R6 artifact hash mismatch")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ScoreR6ArtifactConflictError("Score-R6 artifact is invalid") from exc
        raw["content_hash"] = stored
        return raw


__all__ = ["ScoreR6ArtifactConflictError", "ScoreR6ArtifactStore"]
