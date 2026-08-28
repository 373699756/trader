"""Atomic, tamper-evident artifacts for cost-aware shadow selection."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from trader.application.research.cost_aware_selection_models import CostAwareSelectionReport


class CostAwareSelectionArtifactConflictError(RuntimeError):
    pass


class CostAwareSelectionArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(self, report: CostAwareSelectionReport) -> str:
        path = self._root / report.selection_spec_hash / report.parent_report_hash / "selection-report.json"
        expected = _report_payload(report)
        if path.exists():
            self._verify_existing(path, expected)
            return report.content_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".selection-report-", suffix=".json", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(expected, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                self._verify_existing(path, expected)
        finally:
            temporary.unlink(missing_ok=True)
        return report.content_hash

    @staticmethod
    def _verify_existing(path: Path, expected: dict[str, object]) -> None:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CostAwareSelectionArtifactConflictError("cost-aware selection artifact is tampered") from exc
        if existing != expected:
            raise CostAwareSelectionArtifactConflictError("cost-aware selection artifact is tampered or conflicting")


def _report_payload(report: CostAwareSelectionReport) -> dict[str, object]:
    return {
        "parent_report_hash": report.parent_report_hash,
        "parent_spec_hash": report.parent_spec_hash,
        "selection_spec_hash": report.selection_spec_hash,
        "top_k": report.top_k,
        "maximum_per_industry": report.maximum_per_industry,
        "maximum_board_fraction": report.maximum_board_fraction,
        "d25_entry_threshold": report.d25_entry_threshold,
        "d25_maintenance_threshold": report.d25_maintenance_threshold,
        "tomorrow_entry_threshold": report.tomorrow_entry_threshold,
        "days": [
            {
                "prediction_date": day.prediction_date.isoformat(),
                "horizon": day.horizon,
                "window_mode": day.window_mode,
                "model_family": day.model_family,
                "evaluations": [
                    {
                        "code": item.code,
                        "board": item.board,
                        "industry": item.industry,
                        "gross_expected_excess": item.gross_expected_excess,
                        "estimated_cost": item.estimated_cost,
                        "net_utility": item.net_utility,
                        "severe_loss_probability": item.severe_loss_probability,
                        "uncertainty": item.uncertainty,
                        "incumbent": item.incumbent,
                        "required_threshold": item.required_threshold,
                        "selected_rank": item.selected_rank,
                        "skip_reason": item.skip_reason,
                    }
                    for item in day.evaluations
                ],
                "selected_codes": list(day.selected_codes),
                "content_hash": day.content_hash,
            }
            for day in report.days
        ],
        "status": report.status,
        "production_authority": report.production_authority,
        "schema_version": report.schema_version,
        "content_hash": report.content_hash,
    }


__all__ = ["CostAwareSelectionArtifactConflictError", "CostAwareSelectionArtifactStore"]
