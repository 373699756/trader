"""Immutable JSON persistence for deterministic Score-R3 baseline reports."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.application.research.replay_models import (
    BaselineAggregateMetrics,
    BaselineDayMetrics,
    BaselineReportStatus,
    ScoreR3BaselineReport,
    canonical_hash,
    canonical_json,
    canonical_value,
)

_REPORT_NAME = "score-r3-baseline-report.json"


class BaselineReportConflictError(RuntimeError):
    pass


class JsonBaselineReportStore:
    """Write one report identity once and verify every subsequent read."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, report: ScoreR3BaselineReport) -> ScoreR3BaselineReport:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / _REPORT_NAME
        if path.exists():
            existing = self.verify()
            if existing.report_hash != report.report_hash:
                raise BaselineReportConflictError("Score-R3 report identity conflict")
            return existing
        payload = canonical_value(report)
        if not isinstance(payload, dict):
            raise TypeError("Score-R3 report payload must be an object")
        payload["report_hash"] = report.report_hash
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(canonical_json(payload), encoding="utf-8")
        try:
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.verify()
                if existing.report_hash != report.report_hash:
                    raise BaselineReportConflictError("Score-R3 report identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> ScoreR3BaselineReport:
        path = self._root / _REPORT_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("report payload is not an object")
            stored_hash = raw.pop("report_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("report hash mismatch")
            report = _report_from_payload(raw)
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BaselineReportConflictError("Score-R3 report hash or schema is invalid") from exc
        if report.report_hash != stored_hash:
            raise BaselineReportConflictError("Score-R3 report reconstructed hash mismatch")
        return report


def _report_from_payload(raw: dict[str, object]) -> ScoreR3BaselineReport:
    days_raw = raw["days"]
    aggregate_raw = raw["aggregate"]
    if not isinstance(days_raw, list) or not isinstance(aggregate_raw, dict):
        raise TypeError("Score-R3 report metrics are invalid")
    days = tuple(_day_from_payload(item) for item in days_raw if isinstance(item, dict))
    if len(days) != len(days_raw):
        raise TypeError("Score-R3 day metrics are invalid")
    aggregate = BaselineAggregateMetrics(
        _triple(aggregate_raw["net_excess_returns"]),
        _optional_float(aggregate_raw["mean_mae_atr20"]),
        _optional_float(aggregate_raw["severe_drawdown_rate"]),
        _optional_float(aggregate_raw["candidate_recall"]),
        _required_float(aggregate_raw["field_coverage"]),
        _required_float(aggregate_raw["mean_maximum_board_fraction"]),
        _required_float(aggregate_raw["mean_maximum_industry_fraction"]),
        _optional_float(aggregate_raw["mean_rank_ic"]),
        _optional_five(aggregate_raw["score_bucket_net_excess_20bp"]),
    )
    status = str(raw["status"])
    if status not in {"replayed", "exploratory"}:
        raise ValueError("Score-R3 report status is invalid")
    return ScoreR3BaselineReport(
        cast(BaselineReportStatus, status),
        str(raw["extraction_hash"]),
        _extraction_status(raw["extraction_status"]),
        days,
        aggregate,
        str(raw["schema_version"]),
        str(raw["replay_version"]),
        _triple(raw["cost_rates"]),
    )


def _day_from_payload(raw: dict[str, object]) -> BaselineDayMetrics:
    selected = raw["selected_codes"]
    oracle = raw["oracle_codes"]
    if not isinstance(selected, list) or not isinstance(oracle, list):
        raise TypeError("Score-R3 production or oracle codes are invalid")
    return BaselineDayMetrics(
        date.fromisoformat(str(raw["trade_date"])),
        str(raw["day_hash"]),
        str(raw["input_hash"]),
        tuple(str(item) for item in selected),
        tuple(str(item) for item in oracle),
        _selection_status(raw["selection_status"]),
        _required_int(raw["evaluated_count"]),
        _required_int(raw["oracle_selected_count"]),
        _required_int(raw["recalled_oracle_count"]),
        _triple(raw["net_excess_returns"]),
        _optional_float(raw["mean_mae_atr20"]),
        _optional_float(raw["severe_drawdown_rate"]),
        _optional_float(raw["candidate_recall"]),
        _required_float(raw["field_coverage"]),
        _required_float(raw["maximum_board_fraction"]),
        _required_float(raw["maximum_industry_fraction"]),
        _optional_float(raw["rank_ic"]),
        _optional_five(raw["score_bucket_net_excess_20bp"]),
    )


def _triple(value: object) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise TypeError("Score-R3 triple metric is invalid")
    return float(value[0]), float(value[1]), float(value[2])


def _optional_five(value: object) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    if not isinstance(value, list) or len(value) != 5:
        raise TypeError("Score-R3 quintile metric is invalid")
    return tuple(_optional_float(item) for item in value)  # type: ignore[return-value]


def _optional_float(value: object) -> float | None:
    return None if value is None else _required_float(value)


def _required_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("Score-R3 numeric metric is invalid")
    return float(value)


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Score-R3 count metric is invalid")
    return value


def _selection_status(value: object) -> Literal["selected", "no_decision"]:
    if value not in {"selected", "no_decision"}:
        raise ValueError("Score-R3 selection status is invalid")
    return value


def _extraction_status(value: object) -> Literal["extracted", "exploratory"]:
    if value not in {"extracted", "exploratory"}:
        raise ValueError("Score-R3 extraction status is invalid")
    return value


__all__ = ["BaselineReportConflictError", "JsonBaselineReportStore"]
