"""Immutable JSON persistence for native factor diagnostic reports."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import cast

from trader.application.research.factor_diagnostic_models import (
    DiagnosticStatus,
    FactorAggregateDiagnostic,
    FactorCostQuintiles,
    FactorDailyDiagnostic,
    FactorLagDiagnostic,
    FactorStratumDiagnostic,
    OracleRecallDay,
    OracleRecallDiagnostic,
    QuintileValues,
    ScoreFactorDiagnosticReport,
    StratumDimension,
)
from trader.application.research.replay_models import canonical_hash, canonical_json, canonical_value

_REPORT_NAME = "score-factor-diagnostic-report.json"


class FactorDiagnosticReportConflictError(RuntimeError):
    pass


class JsonFactorDiagnosticReportStore:
    """Write one factor report identity once and verify every subsequent read."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, report: ScoreFactorDiagnosticReport) -> ScoreFactorDiagnosticReport:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / _REPORT_NAME
        if path.exists():
            existing = self.verify()
            if existing.report_hash != report.report_hash:
                raise FactorDiagnosticReportConflictError("factor diagnostic report identity conflict")
            return existing
        payload = canonical_value(report)
        if not isinstance(payload, dict):
            raise TypeError("factor diagnostic report payload must be an object")
        payload["report_hash"] = report.report_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.verify()
                if existing.report_hash != report.report_hash:
                    raise FactorDiagnosticReportConflictError("factor diagnostic report identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> ScoreFactorDiagnosticReport:
        path = self._root / _REPORT_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("factor report payload is not an object")
            stored_hash = raw.pop("report_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("factor report hash mismatch")
            report = _report_from_payload(raw)
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FactorDiagnosticReportConflictError("factor diagnostic report hash or schema is invalid") from exc
        if report.report_hash != stored_hash:
            raise FactorDiagnosticReportConflictError("factor diagnostic report reconstructed hash mismatch")
        return report


def _report_from_payload(raw: dict[str, object]) -> ScoreFactorDiagnosticReport:
    status = str(raw["status"])
    if status not in {"evaluated", "exploratory"}:
        raise ValueError("factor report status is invalid")
    return ScoreFactorDiagnosticReport(
        cast(DiagnosticStatus, status),
        str(raw["extraction_hash"]),
        str(raw["baseline_report_hash"]),
        str(raw["dimension_hash"]),
        str(raw["research_identity"]),
        str(raw["research_spec_hash"]),
        tuple(_factor(item) for item in _objects(raw["factors"], "factors")),
        _oracle(_object(raw["oracle_recall"], "oracle recall")),
        schema_version=str(raw["schema_version"]),
        diagnostic_version=str(raw["diagnostic_version"]),
        cost_rates=_float_triple(raw["cost_rates"]),
        decay_lags=_int_triple(raw["decay_lags"]),
        production_authority=_boolean(raw["production_authority"]),
    )


def _factor(raw: dict[str, object]) -> FactorAggregateDiagnostic:
    return FactorAggregateDiagnostic(
        str(raw["factor_name"]),
        _integer(raw["total_count"]),
        _integer(raw["observed_count"]),
        _number(raw["coverage"]),
        _number(raw["missing_rate"]),
        _optional_number(raw["mean_ic"]),
        _optional_number(raw["mean_rank_ic"]),
        _optional_number(raw["icir"]),
        tuple(_cost(item) for item in _objects(raw["cost_quintiles"], "factor costs")),
        _quintiles(raw["severe_rate_by_quintile"]),
        _quintiles(raw["mean_mae_atr20_by_quintile"]),
        _optional_number(raw["maximum_stock_contribution"]),
        _optional_number(raw["top_five_stock_contribution"]),
        tuple(_lag(item) for item in _objects(raw["lags"], "factor lags")),
        tuple(_stratum(item) for item in _objects(raw["strata"], "factor strata")),
        tuple(_day(item) for item in _objects(raw["days"], "factor days")),
    )


def _cost(raw: dict[str, object]) -> FactorCostQuintiles:
    return FactorCostQuintiles(
        _number(raw["cost_rate"]),
        _quintiles(raw["quintile_net_excess"]),
        _optional_number(raw["adjacent_monotonic_fraction"]),
        _optional_number(raw["top_minus_bottom"]),
    )


def _day(raw: dict[str, object]) -> FactorDailyDiagnostic:
    top_codes = _array(raw["top_quintile_codes"], "top-quintile codes")
    return FactorDailyDiagnostic(
        date.fromisoformat(str(raw["trade_date"])),
        str(raw["day_hash"]),
        str(raw["input_hash"]),
        _integer(raw["total_count"]),
        _integer(raw["observed_count"]),
        _number(raw["coverage"]),
        _number(raw["missing_rate"]),
        _optional_number(raw["ic"]),
        _optional_number(raw["rank_ic"]),
        _five_ints(raw["quintile_counts"]),
        tuple(_cost(item) for item in _objects(raw["cost_quintiles"], "daily costs")),
        _quintiles(raw["severe_rate_by_quintile"]),
        _quintiles(raw["mean_mae_atr20_by_quintile"]),
        tuple(str(item) for item in top_codes),
    )


def _lag(raw: dict[str, object]) -> FactorLagDiagnostic:
    return FactorLagDiagnostic(
        _integer(raw["lag"]),
        _optional_number(raw["decay_rank_ic"]),
        _optional_number(raw["top_quintile_turnover"]),
        _integer(raw["valid_decay_pairs"]),
        _integer(raw["valid_turnover_pairs"]),
    )


def _stratum(raw: dict[str, object]) -> FactorStratumDiagnostic:
    dimension = str(raw["dimension"])
    if dimension not in {"board", "industry", "market_cap", "liquidity"}:
        raise ValueError("factor stratum dimension is invalid")
    return FactorStratumDiagnostic(
        cast(StratumDimension, dimension),
        str(raw["label"]),
        _integer(raw["total_count"]),
        _integer(raw["observed_count"]),
        _number(raw["coverage"]),
        _number(raw["missing_rate"]),
        _optional_number(raw["mean_ic"]),
        _optional_number(raw["mean_rank_ic"]),
        _optional_number(raw["mean_net_excess_20bp"]),
        _optional_number(raw["severe_loss_rate"]),
        _optional_number(raw["mean_mae_atr20"]),
    )


def _oracle(raw: dict[str, object]) -> OracleRecallDiagnostic:
    return OracleRecallDiagnostic(
        _integer(raw["oracle_count"]),
        _integer(raw["pre_pruning_recalled"]),
        _integer(raw["post_pruning_recalled"]),
        _optional_number(raw["pre_pruning_recall"]),
        _optional_number(raw["post_pruning_recall"]),
        tuple(_oracle_day(item) for item in _objects(raw["days"], "oracle days")),
    )


def _oracle_day(raw: dict[str, object]) -> OracleRecallDay:
    return OracleRecallDay(
        date.fromisoformat(str(raw["trade_date"])),
        _integer(raw["oracle_count"]),
        _integer(raw["pre_pruning_recalled"]),
        _integer(raw["post_pruning_recalled"]),
        _optional_number(raw["pre_pruning_recall"]),
        _optional_number(raw["post_pruning_recall"]),
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"factor report {label} must be an object")
    return value


def _objects(value: object, label: str) -> tuple[dict[str, object], ...]:
    return tuple(_object(item, label) for item in _array(value, label))


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"factor report {label} must be an array")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("factor report metric must be numeric")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("factor report count must be an integer")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("factor report authority flag must be boolean")
    return value


def _quintiles(value: object) -> QuintileValues:
    raw = _array(value, "quintiles")
    if len(raw) != 5:
        raise TypeError("factor report quintiles must contain five values")
    return cast(QuintileValues, tuple(_optional_number(item) for item in raw))


def _five_ints(value: object) -> tuple[int, int, int, int, int]:
    raw = _array(value, "quintile counts")
    if len(raw) != 5:
        raise TypeError("factor report quintile counts must contain five values")
    return cast(tuple[int, int, int, int, int], tuple(_integer(item) for item in raw))


def _float_triple(value: object) -> tuple[float, float, float]:
    raw = _array(value, "cost rates")
    if len(raw) != 3:
        raise TypeError("factor report costs must contain three values")
    return _number(raw[0]), _number(raw[1]), _number(raw[2])


def _int_triple(value: object) -> tuple[int, int, int]:
    raw = _array(value, "decay lags")
    if len(raw) != 3:
        raise TypeError("factor report lags must contain three values")
    return _integer(raw[0]), _integer(raw[1]), _integer(raw[2])


__all__ = ["FactorDiagnosticReportConflictError", "JsonFactorDiagnosticReportStore"]
