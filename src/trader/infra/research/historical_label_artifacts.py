"""Immutable JSON storage for the three-strategy label preregistration."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.application.research.replay_models import canonical_hash, canonical_json
from trader.domain.research.h1_point_in_time import H1Strategy
from trader.domain.research.historical_label import (
    HistoricalAnchor,
    HistoricalLabelAggregate,
    HistoricalLabelContract,
    HistoricalLabelPreregistration,
    HistoricalLabelPreregistrationBatch,
    HistoricalPreregistrationStatus,
    HistoricalTemporalSplit,
)


class HistoricalLabelArtifactConflictError(RuntimeError):
    """Raised when a preregistration artifact is missing, tampered, or conflicting."""


class HistoricalLabelArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, batch: HistoricalLabelPreregistrationBatch) -> HistoricalLabelPreregistrationBatch:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / "historical_label_preregistration.json"
        if path.exists():
            existing = self.verify()
            if existing.content_hash != batch.content_hash:
                raise HistoricalLabelArtifactConflictError("historical label artifact identity conflict")
            return existing
        payload = _encode_batch(batch)
        payload["content_hash"] = batch.content_hash
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(canonical_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != batch.content_hash:
                    raise HistoricalLabelArtifactConflictError("historical label artifact identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> HistoricalLabelPreregistrationBatch:
        path = self._root / "historical_label_preregistration.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("historical label artifact is not an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("historical label artifact hash mismatch")
            batch = _decode_batch(raw)
            if batch.content_hash != stored_hash:
                raise ValueError("historical label artifact reconstructed hash mismatch")
            return batch
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoricalLabelArtifactConflictError("historical label artifact schema or hash is invalid") from exc


def _encode_batch(batch: HistoricalLabelPreregistrationBatch) -> dict[str, object]:
    return {
        "strategies": [_encode_preregistration(item) for item in batch.strategies],
        "schema_version": batch.schema_version,
        "production_authority": batch.production_authority,
    }


def _encode_preregistration(item: HistoricalLabelPreregistration) -> dict[str, object]:
    return {
        "strategy": item.strategy,
        "status": item.status,
        "h1_metadata_hash": item.h1_metadata_hash,
        "h1_manifest_hash": item.h1_manifest_hash,
        "universe_hash": item.universe_hash,
        "source_cutoff": item.source_cutoff.isoformat(),
        "label": _encode_label(item.label),
        "split": _encode_split(item.split) if item.split is not None else None,
        "failure_reasons": list(item.failure_reasons),
        "terminal_holdout_status": item.terminal_holdout_status,
        "candidate_results_generated": item.candidate_results_generated,
        "production_authority": item.production_authority,
        "schema_version": item.schema_version,
    }


def _encode_label(label: HistoricalLabelContract) -> dict[str, object]:
    return {
        "strategy": label.strategy,
        "anchor": label.anchor,
        "label_version": label.label_version,
        "horizons": list(label.horizons),
        "aggregate": label.aggregate,
        "benchmark_version": label.benchmark_version,
        "cost_version": label.cost_version,
        "cost_bps": list(label.cost_bps),
        "gate_cost_bps": list(label.gate_cost_bps),
        "stress_cost_bps": label.stress_cost_bps,
        "required_metrics": list(label.required_metrics),
        "parity_dimensions": list(label.parity_dimensions),
        "same_population_required": label.same_population_required,
        "cash_days_in_denominator": label.cash_days_in_denominator,
        "deepseek_history_allowed": label.deepseek_history_allowed,
    }


def _encode_split(split: HistoricalTemporalSplit) -> dict[str, object]:
    return {
        "training_dates": _dates(split.training_dates),
        "first_embargo_dates": _dates(split.first_embargo_dates),
        "confirmation_dates": _dates(split.confirmation_dates),
        "second_embargo_dates": _dates(split.second_embargo_dates),
        "terminal_holdout_dates": _dates(split.terminal_holdout_dates),
        "first_trade_date": split.first_trade_date.isoformat(),
        "last_trade_date": split.last_trade_date.isoformat(),
        "date_set_hash": split.date_set_hash,
        "embargo_days_per_boundary": split.embargo_days_per_boundary,
    }


def _decode_batch(raw: dict[str, object]) -> HistoricalLabelPreregistrationBatch:
    if set(raw) != {"strategies", "schema_version", "production_authority"}:
        raise ValueError("historical label artifact fields are invalid")
    strategies = raw["strategies"]
    if not isinstance(strategies, list) or not all(isinstance(item, dict) for item in strategies):
        raise TypeError("historical label strategies are invalid")
    schema = _string(raw["schema_version"])
    authority = _bool(raw["production_authority"])
    return HistoricalLabelPreregistrationBatch(
        tuple(_decode_preregistration(cast(dict[str, object], item)) for item in strategies),
        schema_version=schema,
        production_authority=authority,
    )


def _decode_preregistration(raw: dict[str, object]) -> HistoricalLabelPreregistration:
    expected = {
        "strategy",
        "status",
        "h1_metadata_hash",
        "h1_manifest_hash",
        "universe_hash",
        "source_cutoff",
        "label",
        "split",
        "failure_reasons",
        "terminal_holdout_status",
        "candidate_results_generated",
        "production_authority",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("historical label preregistration fields are invalid")
    reasons = raw["failure_reasons"]
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise TypeError("historical label failure reasons are invalid")
    split = raw["split"]
    if split is not None and not isinstance(split, dict):
        raise TypeError("historical label split is invalid")
    return HistoricalLabelPreregistration(
        strategy=cast(H1Strategy, _string(raw["strategy"])),
        status=cast(HistoricalPreregistrationStatus, _string(raw["status"])),
        h1_metadata_hash=_string(raw["h1_metadata_hash"]),
        h1_manifest_hash=_string(raw["h1_manifest_hash"]),
        universe_hash=_string(raw["universe_hash"]),
        source_cutoff=date.fromisoformat(_string(raw["source_cutoff"])),
        label=_decode_label(cast(dict[str, object], raw["label"])),
        split=_decode_split(cast(dict[str, object], split)) if split is not None else None,
        failure_reasons=tuple(reasons),
        terminal_holdout_status=cast(Literal["terminal_holdout_not_opened"], _string(raw["terminal_holdout_status"])),
        candidate_results_generated=_bool(raw["candidate_results_generated"]),
        production_authority=_bool(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _decode_label(raw: dict[str, object]) -> HistoricalLabelContract:
    expected = {
        "strategy",
        "anchor",
        "label_version",
        "horizons",
        "aggregate",
        "benchmark_version",
        "cost_version",
        "cost_bps",
        "gate_cost_bps",
        "stress_cost_bps",
        "required_metrics",
        "parity_dimensions",
        "same_population_required",
        "cash_days_in_denominator",
        "deepseek_history_allowed",
    }
    if set(raw) != expected:
        raise ValueError("historical label contract fields are invalid")
    return HistoricalLabelContract(
        strategy=cast(H1Strategy, _string(raw["strategy"])),
        anchor=cast(HistoricalAnchor, _string(raw["anchor"])),
        label_version=_string(raw["label_version"]),
        horizons=_ints(raw["horizons"]),
        aggregate=cast(HistoricalLabelAggregate, _string(raw["aggregate"])),
        benchmark_version=_string(raw["benchmark_version"]),
        cost_version=_string(raw["cost_version"]),
        cost_bps=_three_ints(raw["cost_bps"]),
        gate_cost_bps=_two_ints(raw["gate_cost_bps"]),
        stress_cost_bps=_int(raw["stress_cost_bps"]),
        required_metrics=_strings(raw["required_metrics"]),
        parity_dimensions=_strings(raw["parity_dimensions"]),
        same_population_required=_bool(raw["same_population_required"]),
        cash_days_in_denominator=_bool(raw["cash_days_in_denominator"]),
        deepseek_history_allowed=_bool(raw["deepseek_history_allowed"]),
    )


def _decode_split(raw: dict[str, object]) -> HistoricalTemporalSplit:
    expected = {
        "training_dates",
        "first_embargo_dates",
        "confirmation_dates",
        "second_embargo_dates",
        "terminal_holdout_dates",
        "first_trade_date",
        "last_trade_date",
        "date_set_hash",
        "embargo_days_per_boundary",
    }
    if set(raw) != expected:
        raise ValueError("historical label split fields are invalid")
    return HistoricalTemporalSplit(
        training_dates=_date_values(raw["training_dates"]),
        first_embargo_dates=_date_values(raw["first_embargo_dates"]),
        confirmation_dates=_date_values(raw["confirmation_dates"]),
        second_embargo_dates=_date_values(raw["second_embargo_dates"]),
        terminal_holdout_dates=_date_values(raw["terminal_holdout_dates"]),
        first_trade_date=date.fromisoformat(_string(raw["first_trade_date"])),
        last_trade_date=date.fromisoformat(_string(raw["last_trade_date"])),
        date_set_hash=_string(raw["date_set_hash"]),
        embargo_days_per_boundary=_int(raw["embargo_days_per_boundary"]),
    )


def _dates(values: tuple[date, ...]) -> list[str]:
    return [item.isoformat() for item in values]


def _date_values(value: object) -> tuple[date, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("historical label dates are invalid")
    return tuple(date.fromisoformat(item) for item in value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("historical label strings are invalid")
    return tuple(value)


def _ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise TypeError("historical label integers are invalid")
    return tuple(_int(item) for item in value)


def _three_ints(value: object) -> tuple[int, int, int]:
    values = _ints(value)
    if len(values) != 3:
        raise TypeError("historical label integer width is invalid")
    return values[0], values[1], values[2]


def _two_ints(value: object) -> tuple[int, int]:
    values = _ints(value)
    if len(values) != 2:
        raise TypeError("historical label integer width is invalid")
    return values[0], values[1]


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected string")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected integer")
    return value


__all__ = ["HistoricalLabelArtifactConflictError", "HistoricalLabelArtifactStore"]
