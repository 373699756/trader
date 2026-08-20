"""Append-only, tamper-evident artifacts for Score-R6 candidate and forward evidence."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import cast

from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value
from trader.application.research.score_r6_models import (
    ScoreR6ForwardDay,
    ScoreR6ForwardReport,
    ScoreR6HistoricalReport,
)
from trader.domain.research.score_r6 import ScoreR6ForwardSpec


class ScoreR6ArtifactConflictError(RuntimeError):
    pass


class ScoreR6ArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def seal_historical(self, report: ScoreR6HistoricalReport) -> str:
        path = self._root / report.research_identity / "historical-report.json"
        return self._write(path, report, report.content_hash)

    def read_historical_payload(self) -> dict[str, object] | None:
        path = self._root / "score_r6_historical_v1" / "historical-report.json"
        return self._read_verified(path) if path.is_file() else None

    def register_forward(self, spec: ScoreR6ForwardSpec) -> str:
        path = self._root / spec.research_identity / "forward-spec.json"
        return self._write(path, spec, spec.content_hash)

    def append_forward_day(self, spec: ScoreR6ForwardSpec, day: ScoreR6ForwardDay) -> str:
        if day.research_spec_hash != spec.content_hash or day.trade_date not in spec.planned_trade_dates:
            raise ValueError("Score-R6 forward day does not match its preregistered spec")
        path = self._root / spec.research_identity / "days" / f"{day.trade_date.isoformat()}.json"
        return self._write(path, day, day.content_hash)

    def seal_forward_report(self, spec: ScoreR6ForwardSpec, report: ScoreR6ForwardReport) -> str:
        if report.research_identity != spec.research_identity or report.research_spec_hash != spec.content_hash:
            raise ValueError("Score-R6 forward report does not match its preregistered spec")
        if report.recorded_days != len(spec.planned_trade_dates):
            raise ValueError("Score-R6 only seals a complete fixed-window forward report")
        stored_day_hashes = tuple(
            self._verify(self._root / spec.research_identity / "days" / f"{trade_date.isoformat()}.json")
            for trade_date in spec.planned_trade_dates
        )
        if stored_day_hashes != report.day_hashes:
            raise ScoreR6ArtifactConflictError("Score-R6 forward report day manifest conflict")
        path = self._root / report.research_identity / "forward-report.json"
        return self._write(path, report, report.content_hash)

    def inspect(self) -> dict[str, object]:
        historical_path = self._root / "score_r6_historical_v1" / "historical-report.json"
        historical = self._read_verified(historical_path) if historical_path.is_file() else None
        forward: list[dict[str, object]] = []
        if self._root.is_dir():
            for directory in sorted(self._root.glob("score_r6_forward_*")):
                if not directory.is_dir():
                    continue
                item = self._inspect_forward_directory(directory)
                if item is not None:
                    forward.append(item)
        return {
            "historical_report_hash": historical["content_hash"] if historical is not None else "",
            "historical_gate_passed": bool(historical.get("historical_gate_passed", False))
            if historical is not None
            else False,
            "forward_research": forward,
            "promotion_eligible": any(bool(item["promotion_eligible"]) for item in forward),
        }

    def _inspect_forward_directory(self, directory: Path) -> dict[str, object] | None:
        spec_path = directory / "forward-spec.json"
        if not spec_path.is_file():
            return None
        spec_payload = self._read_verified(spec_path)
        spec = _forward_spec_from_payload(spec_payload)
        if spec.research_identity != directory.name or spec.content_hash != spec_payload["content_hash"]:
            raise ScoreR6ArtifactConflictError("Score-R6 forward spec identity is invalid")
        report_path = directory / "forward-report.json"
        report_payload = self._read_verified(report_path) if report_path.is_file() else None
        report = _forward_report_from_payload(report_payload) if report_payload is not None else None
        _validate_stored_forward_report(spec, report, report_payload)
        days = tuple(sorted((directory / "days").glob("*.json"))) if (directory / "days").is_dir() else ()
        day_hashes = tuple(self._validated_day_hash(path, spec) for path in days)
        if report is not None and day_hashes != report.day_hashes:
            raise ScoreR6ArtifactConflictError("Score-R6 forward report day manifest is invalid")
        return {
            "research_identity": directory.name,
            "research_spec_hash": spec.content_hash,
            "recorded_days": len(days),
            "report_hash": report.content_hash if report is not None else "",
            "status": report.status if report is not None else "forward_collecting",
            "promotion_eligible": report.promotion_eligible if report is not None else False,
            "production_scope": report.production_scope if report is not None else "none",
        }

    def _validated_day_hash(self, path: Path, spec: ScoreR6ForwardSpec) -> str:
        try:
            trade_date = date.fromisoformat(path.stem)
        except ValueError as exc:
            raise ScoreR6ArtifactConflictError("Score-R6 forward day filename is invalid") from exc
        if trade_date not in spec.planned_trade_dates:
            raise ScoreR6ArtifactConflictError("Score-R6 forward day is outside the fixed window")
        return self._verify(path)

    def _write(self, path: Path, value: object, expected_hash: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            stored_hash = self._verify(path)
            if stored_hash != expected_hash:
                raise ScoreR6ArtifactConflictError("Score-R6 artifact identity conflict")
            return stored_hash
        payload = canonical_value(value)
        if not isinstance(payload, dict):
            raise TypeError("Score-R6 artifact must be a JSON object")
        payload["content_hash"] = expected_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                stored_hash = self._verify(path)
                if stored_hash != expected_hash:
                    raise ScoreR6ArtifactConflictError("Score-R6 artifact identity conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        return self._verify(path)

    @staticmethod
    def _verify(path: Path) -> str:
        return str(ScoreR6ArtifactStore._read_verified(path)["content_hash"])

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
            raise ScoreR6ArtifactConflictError("Score-R6 artifact hash or schema is invalid") from exc
        raw["content_hash"] = stored_hash
        return raw


def _forward_spec_from_payload(raw: dict[str, object]) -> ScoreR6ForwardSpec:
    planned_dates = raw.get("planned_trade_dates")
    if not isinstance(planned_dates, list):
        raise ScoreR6ArtifactConflictError("Score-R6 forward spec dates are invalid")
    try:
        return ScoreR6ForwardSpec(
            research_identity=str(raw["research_identity"]),
            preregistered_on=date.fromisoformat(str(raw["preregistered_on"])),
            planned_trade_dates=tuple(date.fromisoformat(str(item)) for item in planned_dates),
            historical_report_hash=str(raw["historical_report_hash"]),
            frozen_candidate_hash=str(raw["frozen_candidate_hash"]),
            trading_calendar_hash=str(raw["trading_calendar_hash"]),
            rule_identity_hash=str(raw["rule_identity_hash"]),
            config_strategy_identity_hash=str(raw["config_strategy_identity_hash"]),
            required_pair_count=_int(raw["required_pair_count"]),
            primary_cost_bps=_int(raw["primary_cost_bps"]),
            primary_block_days=_int(raw["primary_block_days"]),
            minimum_local_gain_pct=_float(raw["minimum_local_gain_pct"]),
            minimum_hybrid_increment_pct=_float(raw["minimum_hybrid_increment_pct"]),
            maximum_local_severe_rate_delta=_float(raw["maximum_local_severe_rate_delta"]),
            maximum_local_turnover_delta=_float(raw["maximum_local_turnover_delta"]),
            maximum_local_stability_delta=_float(raw["maximum_local_stability_delta"]),
            minimum_local_recall=_float(raw["minimum_local_recall"]),
            maximum_local_stock_weight=_float(raw["maximum_local_stock_weight"]),
            maximum_local_board_fraction=_float(raw["maximum_local_board_fraction"]),
            bootstrap_repetitions=_int(raw["bootstrap_repetitions"]),
            bootstrap_alpha=_float(raw["bootstrap_alpha"]),
            promotion_authority=_bool(raw["promotion_authority"]),
            data_schema_version=str(raw["data_schema_version"]),
            strategy_version=str(raw["strategy_version"]),
            fusion_version=str(raw["fusion_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoreR6ArtifactConflictError("Score-R6 forward spec schema is invalid") from exc


def _validate_stored_forward_report(
    spec: ScoreR6ForwardSpec,
    report: ScoreR6ForwardReport | None,
    payload: dict[str, object] | None,
) -> None:
    if report is None:
        return
    if payload is None or report.content_hash != payload["content_hash"]:
        raise ScoreR6ArtifactConflictError("Score-R6 forward report hash identity is invalid")
    if report.research_identity != spec.research_identity or report.research_spec_hash != spec.content_hash:
        raise ScoreR6ArtifactConflictError("Score-R6 forward report research identity is invalid")


def _forward_report_from_payload(raw: dict[str, object]) -> ScoreR6ForwardReport:
    day_hashes = raw.get("day_hashes")
    reasons = raw.get("failure_reasons")
    if not isinstance(day_hashes, list) or not isinstance(reasons, list):
        raise ScoreR6ArtifactConflictError("Score-R6 forward report manifest is invalid")
    try:
        return ScoreR6ForwardReport(
            status=cast(str, raw["status"]),  # type: ignore[arg-type]
            research_identity=str(raw["research_identity"]),
            research_spec_hash=str(raw["research_spec_hash"]),
            recorded_days=_int(raw["recorded_days"]),
            pair_count=_int(raw["pair_count"]),
            day_hashes=tuple(str(item) for item in day_hashes),
            local_mean_gain_pct=_optional_float(raw["local_mean_gain_pct"]),
            local_severe_rate_delta=_optional_float(raw["local_severe_rate_delta"]),
            local_turnover_delta=_optional_float(raw["local_turnover_delta"]),
            local_stability_delta=_optional_float(raw["local_stability_delta"]),
            local_recall=_optional_float(raw["local_recall"]),
            local_maximum_stock_weight=_optional_float(raw["local_maximum_stock_weight"]),
            local_maximum_board_fraction=_optional_float(raw["local_maximum_board_fraction"]),
            hybrid_mean_increment_pct=_optional_float(raw["hybrid_mean_increment_pct"]),
            hybrid_confidence_lower_pct=_optional_float(raw["hybrid_confidence_lower_pct"]),
            hybrid_p_value=_optional_float(raw["hybrid_p_value"]),
            hybrid_bootstrap_seed=_optional_int(raw["hybrid_bootstrap_seed"]),
            local_gate_passed=_bool(raw["local_gate_passed"]),
            hybrid_independent_gain_passed=_bool(raw["hybrid_independent_gain_passed"]),
            production_scope=cast(str, raw["production_scope"]),  # type: ignore[arg-type]
            promotion_eligible=_bool(raw["promotion_eligible"]),
            failure_reasons=tuple(str(item) for item in reasons),
            schema_version=str(raw["schema_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoreR6ArtifactConflictError("Score-R6 forward report schema is invalid") from exc


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Score-R6 optional numeric value is invalid")
    return float(value)


def _float(value: object) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        raise TypeError("Score-R6 required numeric value is invalid")
    return parsed


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Score-R6 required integer value is invalid")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("Score-R6 boolean value is invalid")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Score-R6 optional integer value is invalid")
    return value


__all__ = ["ScoreR6ArtifactConflictError", "ScoreR6ArtifactStore"]
