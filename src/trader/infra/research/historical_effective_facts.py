"""Immutable JSON boundary for historical effective-facts capability evidence."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import cast

from trader.domain.research.historical_effective_facts import (
    HistoricalEffectiveFactsAudit,
    HistoricalEffectiveFactsProbe,
    HistoricalEffectiveFactsStatus,
)


class HistoricalEffectiveFactsArtifactConflictError(RuntimeError):
    """Raised when effective-facts capability evidence conflicts or is corrupt."""


class HistoricalEffectiveFactsArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "historical-effective-facts-capability.json"

    def write(self, report: HistoricalEffectiveFactsAudit) -> HistoricalEffectiveFactsAudit:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != report.content_hash:
                raise HistoricalEffectiveFactsArtifactConflictError("effective-facts artifact identity conflict")
            return existing
        payload = _encode(report)
        payload["content_hash"] = report.content_hash
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".historical-effective-facts.", suffix=".tmp", dir=self._root
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
                if existing.content_hash != report.content_hash:
                    raise HistoricalEffectiveFactsArtifactConflictError(
                        "effective-facts artifact identity conflict"
                    ) from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> HistoricalEffectiveFactsAudit:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
                raise TypeError("effective-facts artifact is not an object")
            payload = cast(dict[str, object], raw)
            stored_hash = payload.pop("content_hash")
            report = _decode(payload)
            if not isinstance(stored_hash, str) or report.content_hash != stored_hash:
                raise ValueError("effective-facts artifact hash mismatch")
            return report
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HistoricalEffectiveFactsArtifactConflictError(
                "effective-facts artifact schema or hash is invalid"
            ) from exc


def _encode(report: HistoricalEffectiveFactsAudit) -> dict[str, object]:
    return {
        "probes": [
            {
                "source": item.source,
                "earliest_available": item.earliest_available.isoformat()
                if item.earliest_available is not None
                else None,
                "industry_effective_at": item.industry_effective_at,
                "eligibility_effective_at": item.eligibility_effective_at,
                "hard_filter_effective_at": item.hard_filter_effective_at,
                "risk_facts_effective_at": item.risk_facts_effective_at,
                "schema_version": item.schema_version,
            }
            for item in report.probes
        ],
        "status": report.status,
        "failure_reasons": list(report.failure_reasons),
        "point_in_time_parity": report.point_in_time_parity,
        "v3_training_authority": report.v3_training_authority,
        "production_authority": report.production_authority,
        "schema_version": report.schema_version,
    }


def _decode(raw: dict[str, object]) -> HistoricalEffectiveFactsAudit:
    expected = {
        "probes",
        "status",
        "failure_reasons",
        "point_in_time_parity",
        "v3_training_authority",
        "production_authority",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("effective-facts artifact fields are invalid")
    probes = raw["probes"]
    reasons = raw["failure_reasons"]
    if (
        not isinstance(probes, list)
        or not isinstance(reasons, list)
        or not all(isinstance(item, str) for item in reasons)
    ):
        raise TypeError("effective-facts artifact collections are invalid")
    return HistoricalEffectiveFactsAudit(
        probes=tuple(_decode_probe(item) for item in probes),
        status=cast(HistoricalEffectiveFactsStatus, _string(raw["status"])),
        failure_reasons=tuple(cast(list[str], reasons)),
        point_in_time_parity=_boolean(raw["point_in_time_parity"]),
        v3_training_authority=_boolean(raw["v3_training_authority"]),
        production_authority=_boolean(raw["production_authority"]),
        schema_version=_string(raw["schema_version"]),
    )


def _decode_probe(value: object) -> HistoricalEffectiveFactsProbe:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError("effective-facts probe is invalid")
    raw = cast(dict[str, object], value)
    expected = {
        "source",
        "earliest_available",
        "industry_effective_at",
        "eligibility_effective_at",
        "hard_filter_effective_at",
        "risk_facts_effective_at",
        "schema_version",
    }
    if set(raw) != expected:
        raise ValueError("effective-facts probe fields are invalid")
    earliest = raw["earliest_available"]
    if earliest is not None and not isinstance(earliest, str):
        raise TypeError("effective-facts earliest date is invalid")
    return HistoricalEffectiveFactsProbe(
        source=_string(raw["source"]),
        earliest_available=date.fromisoformat(earliest) if earliest is not None else None,
        industry_effective_at=_boolean(raw["industry_effective_at"]),
        eligibility_effective_at=_boolean(raw["eligibility_effective_at"]),
        hard_filter_effective_at=_boolean(raw["hard_filter_effective_at"]),
        risk_facts_effective_at=_boolean(raw["risk_facts_effective_at"]),
        schema_version=_string(raw["schema_version"]),
    )


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("effective-facts string field is invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("effective-facts boolean field is invalid")
    return value


__all__ = [
    "HistoricalEffectiveFactsArtifactConflictError",
    "HistoricalEffectiveFactsArtifactStore",
]
