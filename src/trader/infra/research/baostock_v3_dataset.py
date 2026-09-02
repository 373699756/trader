"""Immutable JSON artifact boundary for the Tomorrow V3 BaoStock dataset."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import cast

from trader.domain.research.baostock_daily import (
    BaoStockV3DatasetManifest,
    BaoStockV3DatasetStatus,
    BaoStockV3LabelContract,
    BaoStockV3Split,
)


class BaoStockV3DatasetArtifactConflictError(RuntimeError):
    """Raised when an existing dataset artifact conflicts or is corrupt."""


class BaoStockV3DatasetArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "tomorrow-v3-baostock-dataset.json"

    def write(self, manifest: BaoStockV3DatasetManifest) -> BaoStockV3DatasetManifest:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != manifest.content_hash:
                raise BaoStockV3DatasetArtifactConflictError("dataset artifact identity conflict")
            return existing

        payload = _encode_manifest(manifest)
        payload["content_hash"] = manifest.content_hash
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".tomorrow-v3-baostock-dataset.", suffix=".tmp", dir=self._root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != manifest.content_hash:
                    raise BaoStockV3DatasetArtifactConflictError("dataset artifact identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> BaoStockV3DatasetManifest:
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            raw = _object(loaded, "dataset")
            stored_hash = raw.pop("content_hash")
            manifest = _decode_manifest(raw)
            if not isinstance(stored_hash, str) or manifest.content_hash != stored_hash:
                raise ValueError("dataset artifact hash mismatch")
            return manifest
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BaoStockV3DatasetArtifactConflictError("dataset artifact schema or hash is invalid") from exc


def _encode_manifest(value: BaoStockV3DatasetManifest) -> dict[str, object]:
    return {
        "daily_manifest_hash": value.daily_manifest_hash,
        "effective_facts_hash": value.effective_facts_hash,
        "label_contract": _encode_label_contract(value.label_contract),
        "status": value.status,
        "split": _encode_split(value.split) if value.split is not None else None,
        "failure_reasons": list(value.failure_reasons),
        "point_in_time_parity": value.point_in_time_parity,
        "production_authority": value.production_authority,
        "terminal_holdout_opened": value.terminal_holdout_opened,
        "schema_version": value.schema_version,
    }


def _decode_manifest(raw: dict[str, object]) -> BaoStockV3DatasetManifest:
    _fields(
        raw,
        {
            "daily_manifest_hash",
            "effective_facts_hash",
            "label_contract",
            "status",
            "split",
            "failure_reasons",
            "point_in_time_parity",
            "production_authority",
            "terminal_holdout_opened",
            "schema_version",
        },
        "dataset",
    )
    split = raw["split"]
    return BaoStockV3DatasetManifest(
        daily_manifest_hash=_string(raw["daily_manifest_hash"]),
        effective_facts_hash=_string(raw["effective_facts_hash"]),
        label_contract=_decode_label_contract(_object(raw["label_contract"], "label contract")),
        status=cast(BaoStockV3DatasetStatus, _string(raw["status"])),
        split=_decode_split(_object(split, "split")) if split is not None else None,
        failure_reasons=_strings(raw["failure_reasons"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        production_authority=_boolean(raw["production_authority"]),
        terminal_holdout_opened=_boolean(raw["terminal_holdout_opened"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_label_contract(value: BaoStockV3LabelContract) -> dict[str, object]:
    return {
        "formula": value.formula,
        "primary_cost_bps": value.primary_cost_bps,
        "gate_cost_bps": value.gate_cost_bps,
        "stress_cost_bps": value.stress_cost_bps,
        "label_pending_required": value.label_pending_required,
        "schema_version": value.schema_version,
    }


def _decode_label_contract(raw: dict[str, object]) -> BaoStockV3LabelContract:
    _fields(
        raw,
        {
            "formula",
            "primary_cost_bps",
            "gate_cost_bps",
            "stress_cost_bps",
            "label_pending_required",
            "schema_version",
        },
        "label contract",
    )
    return BaoStockV3LabelContract(
        formula=_string(raw["formula"]),
        primary_cost_bps=_integer(raw["primary_cost_bps"]),
        gate_cost_bps=_integer(raw["gate_cost_bps"]),
        stress_cost_bps=_integer(raw["stress_cost_bps"]),
        label_pending_required=_boolean(raw["label_pending_required"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_split(value: BaoStockV3Split) -> dict[str, object]:
    return {
        "parent_manifest_hash": value.parent_manifest_hash,
        "label_contract": _encode_label_contract(value.label_contract),
        "model_fit_dates": _encode_dates(value.model_fit_dates),
        "early_stopping_dates": _encode_dates(value.early_stopping_dates),
        "calibration_dates": _encode_dates(value.calibration_dates),
        "development_dates": _encode_dates(value.development_dates),
        "first_embargo_dates": _encode_dates(value.first_embargo_dates),
        "confirmation_dates": _encode_dates(value.confirmation_dates),
        "second_embargo_dates": _encode_dates(value.second_embargo_dates),
        "daily_proxy_holdout_dates": _encode_dates(value.daily_proxy_holdout_dates),
        "point_in_time_holdout_dates": _encode_dates(value.point_in_time_holdout_dates),
        "training_anchor": value.training_anchor,
        "point_in_time_parity": value.point_in_time_parity,
        "terminal_holdout_opened": value.terminal_holdout_opened,
        "production_authority": value.production_authority,
        "schema_version": value.schema_version,
    }


def _decode_split(raw: dict[str, object]) -> BaoStockV3Split:
    date_fields = {
        "model_fit_dates",
        "early_stopping_dates",
        "calibration_dates",
        "development_dates",
        "first_embargo_dates",
        "confirmation_dates",
        "second_embargo_dates",
        "daily_proxy_holdout_dates",
        "point_in_time_holdout_dates",
    }
    _fields(
        raw,
        date_fields
        | {
            "parent_manifest_hash",
            "label_contract",
            "training_anchor",
            "point_in_time_parity",
            "terminal_holdout_opened",
            "production_authority",
            "schema_version",
        },
        "split",
    )
    return BaoStockV3Split(
        parent_manifest_hash=_string(raw["parent_manifest_hash"]),
        label_contract=_decode_label_contract(_object(raw["label_contract"], "split label contract")),
        model_fit_dates=_dates(raw["model_fit_dates"]),
        early_stopping_dates=_dates(raw["early_stopping_dates"]),
        calibration_dates=_dates(raw["calibration_dates"]),
        development_dates=_dates(raw["development_dates"]),
        first_embargo_dates=_dates(raw["first_embargo_dates"]),
        confirmation_dates=_dates(raw["confirmation_dates"]),
        second_embargo_dates=_dates(raw["second_embargo_dates"]),
        daily_proxy_holdout_dates=_dates(raw["daily_proxy_holdout_dates"]),
        point_in_time_holdout_dates=_dates(raw["point_in_time_holdout_dates"]),
        training_anchor=_string(raw["training_anchor"]),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        terminal_holdout_opened=_boolean(raw["terminal_holdout_opened"]),
        production_authority=_boolean(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _encode_dates(values: tuple[date, ...]) -> list[str]:
    return [value.isoformat() for value in values]


def _dates(value: object) -> tuple[date, ...]:
    return tuple(date.fromisoformat(item) for item in _strings(value))


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"BaoStock V3 {label} is invalid")
    return cast(dict[str, object], value)


def _fields(raw: dict[str, object], expected: set[str], label: str) -> None:
    if set(raw) != expected:
        raise ValueError(f"BaoStock V3 {label} fields are invalid")


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("BaoStock V3 string field is invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("BaoStock V3 boolean field is invalid")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("BaoStock V3 integer field is invalid")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("BaoStock V3 string list is invalid")
    return tuple(value)


__all__ = [
    "BaoStockV3DatasetArtifactConflictError",
    "BaoStockV3DatasetArtifactStore",
]
