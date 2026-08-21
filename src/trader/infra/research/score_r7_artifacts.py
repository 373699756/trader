"""Append-only, tamper-evident Score-R7 human-review dossiers."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import cast

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.score_r7_models import (
    ScoreR7GateResult,
    ScoreR7ParameterProposal,
    ScoreR7PromotionDossier,
    ScoreR7SampleCounts,
    ScoreR7SensitivityResult,
)


class ScoreR7ArtifactConflictError(RuntimeError):
    pass


class ScoreR7ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal(self, dossier: ScoreR7PromotionDossier) -> str:
        path = self._root / dossier.dossier_identity / "promotion-dossier.json"
        if path.exists():
            stored = self._read_dossier(path)
            if stored.content_hash != dossier.content_hash:
                raise ScoreR7ArtifactConflictError("Score-R7 dossier identity conflict")
            return stored.content_hash
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_value(dossier)
        if not isinstance(payload, dict):
            raise TypeError("Score-R7 dossier must be a JSON object")
        payload["content_hash"] = dossier.content_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                stored = self._read_dossier(path)
                if stored.content_hash != dossier.content_hash:
                    raise ScoreR7ArtifactConflictError("Score-R7 dossier identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        return self._read_dossier(path).content_hash

    def inspect(self) -> dict[str, object]:
        dossiers: list[dict[str, object]] = []
        if self._root.is_dir():
            for path in sorted(self._root.glob("*/promotion-dossier.json")):
                dossier = self._read_dossier(path)
                dossiers.append(
                    {
                        "dossier_identity": dossier.dossier_identity,
                        "source_research_identity": dossier.source_research_identity,
                        "content_hash": dossier.content_hash,
                        "production_scope": dossier.production_scope,
                        "manual_review_status": dossier.manual_review_status,
                        "production_change_authorized": dossier.production_change_authorized,
                    }
                )
        return {"dossiers": dossiers, "dossier_count": len(dossiers)}

    @staticmethod
    def _read_dossier(path: Path) -> ScoreR7PromotionDossier:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("artifact payload is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("artifact hash mismatch")
            dossier = _dossier_from_payload(raw)
            if dossier.content_hash != stored_hash:
                raise ValueError("artifact schema hash mismatch")
            return dossier
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ScoreR7ArtifactConflictError("Score-R7 dossier hash or schema is invalid") from exc


def _dossier_from_payload(raw: dict[str, object]) -> ScoreR7PromotionDossier:
    proposal_raw = _dict(raw["proposed_parameters"])
    sensitivity_raw = _list(raw["sensitivity"])
    counts_raw = _dict(raw["sample_counts"])
    board_weights = tuple(_board_weights(item) for item in _list(proposal_raw["board_weight_units"]))
    proposal = ScoreR7ParameterProposal(
        candidate_hash=str(proposal_raw["candidate_hash"]),
        component_names=tuple(str(value) for value in _list(proposal_raw["component_names"])),
        board_weight_units=board_weights,
        action_threshold=_int(proposal_raw["action_threshold"]),
        risk_penalty=_int(proposal_raw["risk_penalty"]),
    )
    sensitivity = tuple(
        ScoreR7SensitivityResult(
            cost_bps=_int(item["cost_bps"]),
            block_days=_int(item["block_days"]),
            sample_days=_int(item["sample_days"]),
            local_mean_gain_pct=_float(item["local_mean_gain_pct"]),
            local_confidence_lower_pct=_float(item["local_confidence_lower_pct"]),
            local_confidence_upper_pct=_float(item["local_confidence_upper_pct"]),
            local_p_value=_float(item["local_p_value"]),
            local_bootstrap_seed=_int(item["local_bootstrap_seed"]),
            hybrid_mean_increment_pct=_float(item["hybrid_mean_increment_pct"]),
            hybrid_confidence_lower_pct=_float(item["hybrid_confidence_lower_pct"]),
            hybrid_confidence_upper_pct=_float(item["hybrid_confidence_upper_pct"]),
            hybrid_p_value=_float(item["hybrid_p_value"]),
            hybrid_bootstrap_seed=_int(item["hybrid_bootstrap_seed"]),
        )
        for item in (_dict(value) for value in sensitivity_raw)
    )
    return ScoreR7PromotionDossier(
        dossier_identity=str(raw["dossier_identity"]),
        source_research_identity=str(raw["source_research_identity"]),
        historical_report_hash=str(raw["historical_report_hash"]),
        forward_spec_hash=str(raw["forward_spec_hash"]),
        forward_report_hash=str(raw["forward_report_hash"]),
        day_manifest_hashes=tuple(str(value) for value in _list(raw["day_manifest_hashes"])),
        trading_calendar_hash=str(raw["trading_calendar_hash"]),
        rule_identity_hash=str(raw["rule_identity_hash"]),
        config_strategy_identity_hash=str(raw["config_strategy_identity_hash"]),
        data_schema_version=str(raw["data_schema_version"]),
        strategy_version=str(raw["strategy_version"]),
        fusion_version=str(raw["fusion_version"]),
        engine_version=str(raw["engine_version"]),
        statistical_program_version=str(raw["statistical_program_version"]),
        production_scope=cast(str, raw["production_scope"]),  # type: ignore[arg-type]
        proposed_parameters=proposal,
        sensitivity=sensitivity,
        gate_results=tuple(_gate_result(item) for item in _list(raw["gate_results"])),
        failed_trade_dates=tuple(date.fromisoformat(str(value)) for value in _list(raw["failed_trade_dates"])),
        sample_counts=ScoreR7SampleCounts(
            planned_days=_int(counts_raw["planned_days"]),
            valid_days=_int(counts_raw["valid_days"]),
            failed_days=_int(counts_raw["failed_days"]),
            pair_count=_int(counts_raw["pair_count"]),
        ),
        ablation_ids=tuple(str(value) for value in _list(raw["ablation_ids"])),
        maximum_stock_weight=_float(raw["maximum_stock_weight"]),
        maximum_board_fraction=_float(raw["maximum_board_fraction"]),
        residual_risks=tuple(str(value) for value in _list(raw["residual_risks"])),
        manual_review_status=cast(str, raw["manual_review_status"]),  # type: ignore[arg-type]
        production_change_authorized=_bool(raw["production_change_authorized"]),  # type: ignore[arg-type]
        schema_version=str(raw["schema_version"]),
    )


def _dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Score-R7 object field is invalid")
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError("Score-R7 list field is invalid")
    return value


def _board_weights(value: object) -> tuple[str, tuple[int, ...]]:
    item = _list(value)
    if len(item) != 2:
        raise TypeError("Score-R7 board weight entry is invalid")
    return str(item[0]), tuple(_int(weight) for weight in _list(item[1]))


def _gate_result(value: object) -> ScoreR7GateResult:
    item = _dict(value)
    return ScoreR7GateResult(
        gate_id=str(item["gate_id"]),
        actual_value=_float(item["actual_value"]),
        comparison=cast(str, item["comparison"]),  # type: ignore[arg-type]
        threshold=_float(item["threshold"]),
        passed=_bool(item["passed"]),
        required_for_scope=_bool(item["required_for_scope"]),
    )


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Score-R7 integer field is invalid")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Score-R7 numeric field is invalid")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Score-R7 boolean field is invalid")
    return value


__all__ = ["ScoreR7ArtifactConflictError", "ScoreR7ArtifactStore"]
