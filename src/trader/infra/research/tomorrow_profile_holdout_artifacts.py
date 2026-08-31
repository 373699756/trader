"""Immutable storage for the one-shot V1/V2 H0 holdout report."""

from __future__ import annotations

import json
import os
from pathlib import Path

from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.application.research.tomorrow_historical_p2_models import TomorrowHistoricalP2GateMetrics
from trader.application.research.tomorrow_profile_holdout import (
    TomorrowProfileHoldoutMetrics,
    TomorrowProfileHoldoutReport,
)


class TomorrowProfileHoldoutArtifactConflictError(RuntimeError):
    pass


class TomorrowProfileHoldoutArtifactStore:
    def __init__(self, runtime_root: Path) -> None:
        self._path = runtime_root / "score-tomorrow-profile" / "v1-v2-h0-holdout-v2.json"

    def seal(self, report: TomorrowProfileHoldoutReport) -> str:
        payload = holdout_report_payload(report)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.read_payload()
            if existing is None or existing.get("content_hash") != report.content_hash:
                raise TomorrowProfileHoldoutArtifactConflictError("Tomorrow profile holdout identity conflict")
            return report.content_hash
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                existing = self.read_payload()
                if existing is None or existing.get("content_hash") != report.content_hash:
                    raise TomorrowProfileHoldoutArtifactConflictError(
                        "Tomorrow profile holdout identity conflict"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)
        return report.content_hash

    def read_payload(self) -> dict[str, object] | None:
        if not self._path.is_file():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("Tomorrow profile holdout artifact is not an object")
            stored = raw.pop("content_hash")
            if not isinstance(stored, str) or canonical_hash(raw) != stored:
                raise ValueError("Tomorrow profile holdout hash mismatch")
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TomorrowProfileHoldoutArtifactConflictError("Tomorrow profile holdout artifact is invalid") from exc
        raw["content_hash"] = stored
        return raw

    def inspect(self) -> dict[str, object]:
        payload = self.read_payload()
        if payload is None:
            return {
                "status": "not_run",
                "report_hash": "",
                "production_authority": False,
            }
        v1 = payload.get("v1")
        v2 = payload.get("v2")
        return {
            "status": payload.get("status", "invalid"),
            "report_hash": payload.get("content_hash", ""),
            "validation_trade_dates": payload.get("validation_trade_dates", 0),
            "validation_pairs": payload.get("validation_pairs", 0),
            "historical_daily_difference_std_pct": payload.get("historical_daily_difference_std_pct"),
            "historical_long_run_difference_std_pct": payload.get("historical_long_run_difference_std_pct"),
            "v1_failure_reasons": v1.get("failure_reasons", []) if isinstance(v1, dict) else [],
            "v2_failure_reasons": v2.get("failure_reasons", []) if isinstance(v2, dict) else [],
            "production_authority": False,
        }


def holdout_report_payload(report: TomorrowProfileHoldoutReport) -> dict[str, object]:
    return {
        "source_spec_hash": report.source_spec_hash,
        "source_manifest_hash": report.source_manifest_hash,
        "validation_evidence_hash": report.validation_evidence_hash,
        "validation_trade_dates": report.validation_trade_dates,
        "validation_pairs": report.validation_pairs,
        "v1": _profile_payload(report.v1),
        "v2": _profile_payload(report.v2),
        "daily_v2_minus_v1_20bp": list(report.daily_v2_minus_v1_20bp),
        "historical_daily_difference_std_pct": report.historical_daily_difference_std_pct,
        "historical_long_run_difference_std_pct": report.historical_long_run_difference_std_pct,
        "status": report.status,
        "production_authority": report.production_authority,
        "schema_version": report.schema_version,
        "content_hash": report.content_hash,
    }


def _profile_payload(value: TomorrowProfileHoldoutMetrics) -> dict[str, object]:
    return {
        "profile_id": value.profile_id,
        "model_id": value.model_id,
        "model_hash": value.model_hash,
        "gates": _gate_payload(value.gates),
        "failure_reasons": list(value.failure_reasons),
    }


def _gate_payload(value: TomorrowHistoricalP2GateMetrics) -> dict[str, object]:
    return {
        "archive_coverage": value.archive_coverage,
        "training_trade_dates": value.training_trade_dates,
        "validation_trade_dates": value.validation_trade_dates,
        "validation_pairs": value.validation_pairs,
        "mean_net_increment_20bp": value.mean_net_increment_20bp,
        "mean_net_increment_50bp": value.mean_net_increment_50bp,
        "mean_net_increment_100bp": value.mean_net_increment_100bp,
        "bootstrap_lower_bound_20bp": value.bootstrap_lower_bound_20bp,
        "baseline_severe_loss_rate": value.baseline_severe_loss_rate,
        "candidate_severe_loss_rate": value.candidate_severe_loss_rate,
        "turnover_increase": value.turnover_increase,
        "mean_rank_ic": value.mean_rank_ic,
        "top_bottom_quintile_spread": value.top_bottom_quintile_spread,
        "maximum_stock_positive_fraction": value.maximum_stock_positive_fraction,
        "top_five_positive_fraction": value.top_five_positive_fraction,
        "maximum_board_fraction": value.maximum_board_fraction,
    }


__all__ = [
    "TomorrowProfileHoldoutArtifactConflictError",
    "TomorrowProfileHoldoutArtifactStore",
    "holdout_report_payload",
]
