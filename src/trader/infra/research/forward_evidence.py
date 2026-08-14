"""Append-only JSON persistence for Score-R5 fixed forward evidence."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import cast

from trader.application.research.challenger_models import (
    ChallengerCandidateOverride,
    ChallengerDayReplay,
    ChallengerSameStockPair,
    HybridSource,
)
from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.score_r5_models import (
    ForwardRecordStatus,
    ScoreR5ForwardBindings,
    ScoreR5ForwardDayRecord,
    score_r5_forward_dates,
)
from trader.domain.research.challengers import ChallengerVariantId
from trader.domain.research.historical import CostSettlementBasis, ResearchBoard
from trader.domain.research.statistics import VARIANT_FAMILY


class ForwardEvidenceConflictError(RuntimeError):
    pass


class JsonScoreR5ForwardStore:
    """Persist one immutable file for each variant and planned date."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def read(self, variant_id: str, trade_date: date) -> ScoreR5ForwardDayRecord | None:
        path = self._path(variant_id, trade_date)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("forward evidence payload is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("forward evidence hash mismatch")
            record = _record_from_payload(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ForwardEvidenceConflictError("Score-R5 forward evidence hash or schema is invalid") from exc
        if record.content_hash != stored_hash:
            raise ForwardEvidenceConflictError("Score-R5 reconstructed forward evidence hash mismatch")
        return record

    def append(self, record: ScoreR5ForwardDayRecord) -> ScoreR5ForwardDayRecord:
        path = self._path(record.bindings.variant_id, record.planned_trade_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.read(record.bindings.variant_id, record.planned_trade_date)
        if existing is not None:
            if existing.content_hash != record.content_hash:
                raise ForwardEvidenceConflictError("Score-R5 forward evidence identity conflict")
            return existing
        payload = canonical_value(record)
        if not isinstance(payload, dict):
            raise TypeError("Score-R5 forward evidence payload must be an object")
        payload["content_hash"] = record.content_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.read(record.bindings.variant_id, record.planned_trade_date)
                if existing is None or existing.content_hash != record.content_hash:
                    raise ForwardEvidenceConflictError("Score-R5 forward evidence identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        stored = self.read(record.bindings.variant_id, record.planned_trade_date)
        if stored is None:
            raise ForwardEvidenceConflictError("Score-R5 forward evidence was not durably created")
        return stored

    def _path(self, variant_id: str, trade_date: date) -> Path:
        if variant_id not in VARIANT_FAMILY:
            raise ValueError("Score-R5 forward store requires a preregistered variant")
        if trade_date not in score_r5_forward_dates():
            raise ValueError("Score-R5 forward store date is outside the fixed window")
        return self._root / variant_id / f"{trade_date.isoformat()}.json"


def _record_from_payload(raw: dict[str, object]) -> ScoreR5ForwardDayRecord:
    bindings_raw = _object(raw["bindings"])
    day_raw = raw["day"]
    oracle_raw = raw["oracle_codes"]
    if not isinstance(oracle_raw, list):
        raise TypeError("Score-R5 forward oracle codes are invalid")
    status = _status(raw["status"])
    return ScoreR5ForwardDayRecord(
        _bindings_from_payload(bindings_raw),
        date.fromisoformat(str(raw["planned_trade_date"])),
        status,
        None if day_raw is None else _day_from_payload(_object(day_raw)),
        tuple(str(item) for item in oracle_raw),
        None if raw["failure_reason"] is None else str(raw["failure_reason"]),
        str(raw["schema_version"]),
    )


def _bindings_from_payload(raw: dict[str, object]) -> ScoreR5ForwardBindings:
    return ScoreR5ForwardBindings(
        str(raw["historical_gate_hash"]),
        cast(ChallengerVariantId, str(raw["variant_id"])),
        str(raw["variant_version"]),
        str(raw["parameter_manifest_hash"]),
        str(raw["data_identity_hash"]),
        str(raw["rule_identity_hash"]),
        str(raw["config_strategy_identity_hash"]),
        str(raw["strategy_version"]),
        str(raw["fusion_version"]),
        str(raw["statistics_version"]),
        str(raw["report_version"]),
    )


def _day_from_payload(raw: dict[str, object]) -> ChallengerDayReplay:
    overrides_raw = raw["overrides"]
    pairs_raw = raw["pairs"]
    if not isinstance(overrides_raw, list) or not isinstance(pairs_raw, list):
        raise TypeError("Score-R5 forward replay rows are invalid")
    return ChallengerDayReplay(
        date.fromisoformat(str(raw["trade_date"])),
        str(raw["day_hash"]),
        str(raw["input_hash"]),
        tuple(_override_from_payload(_object(item)) for item in overrides_raw),
        tuple(_pair_from_payload(_object(item)) for item in pairs_raw),
        cast(str, raw["local_status"]),  # type: ignore[arg-type]
        cast(str, raw["hybrid_status"]),  # type: ignore[arg-type]
    )


def _override_from_payload(raw: dict[str, object]) -> ChallengerCandidateOverride:
    reasons = raw["observe_reasons"]
    if not isinstance(reasons, list):
        raise TypeError("Score-R5 forward override reasons are invalid")
    return ChallengerCandidateOverride(
        str(raw["code"]),
        _optional_float(raw["continuous_entry_score"]),
        cast(str, raw["continuous_entry_status"]),  # type: ignore[arg-type]
        _optional_float(raw["coverage_shrunk_score"]),
        _bool(raw["active_set_expanded"]),
        _bool(raw["selection_eligible"]),
        _bool(raw["force_observe_only"]),
        tuple(str(item) for item in reasons),
    )


def _pair_from_payload(raw: dict[str, object]) -> ChallengerSameStockPair:
    settlement = _object(raw["settlement"])
    return ChallengerSameStockPair(
        str(raw["code"]),
        cast(ResearchBoard, str(raw["board"])),
        _optional_int(raw["production_rank"]),
        _optional_int(raw["local_rank"]),
        _optional_int(raw["hybrid_rank"]),
        _float(raw["production_weight"]),
        _float(raw["local_weight"]),
        _float(raw["hybrid_weight"]),
        _float(raw["local_score"]),
        _float(raw["hybrid_score"]),
        cast(HybridSource, str(raw["hybrid_source"])),
        CostSettlementBasis(
            str(settlement["code"]),
            cast(ResearchBoard, str(settlement["board"])),
            date.fromisoformat(str(settlement["decision_date"])),
            date.fromisoformat(str(settlement["label_date"])),
            _float(settlement["gross_excess_return"]),
            _float(settlement["mae_atr20"]),
            _float(settlement["turnover"]),
        ),
    )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Score-R5 JSON object is invalid")
    return value


def _status(value: object) -> ForwardRecordStatus:
    if value not in {"valid", "failed", "no_decision"}:
        raise ValueError("Score-R5 forward status is invalid")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Score-R5 numeric value is invalid")
    return float(value)


def _optional_float(value: object) -> float | None:
    return None if value is None else _float(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Score-R5 integer value is invalid")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Score-R5 boolean value is invalid")
    return value


__all__ = ["ForwardEvidenceConflictError", "JsonScoreR5ForwardStore"]
