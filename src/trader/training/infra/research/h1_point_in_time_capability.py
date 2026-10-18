"""Read-only supplier capability probes and immutable H1 audit storage."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import cast

from trader.download.application.history_source_capability import HistorySourceCapability, HistorySourceSample
from trader.infra.serialization.canonical import CanonicalJsonText
from trader.infra.serialization.fields import IsBoolean, IsInteger, IsTextSequence
from trader.infra.serialization.sealing import PublishImmutable
from trader.training.domain.evaluation.h1_point_in_time import (
    H1_SOURCE_CUTOFF,
    BuildH1CapabilityAudit,
    H1CapabilityAuditReport,
    H1CapabilityProbe,
    H1CapabilityStrategyStatus,
    H1CoverageState,
    ResearchStrategy,
)
from trader.training.domain.evaluation.research_identity import CanonicalResearchHash


class H1CapabilityEvidenceConflictError(RuntimeError):
    """Raised when a capability artifact is missing, tampered, or conflicting."""


class FreeSourceH1CapabilityProbe:
    """Probe only bounded metadata; supplier payload values are never retained."""

    def __init__(self, source: HistorySourceCapability, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("H1 capability timeout must be positive")
        self._source = source
        self._timeout = timeout_seconds

    def run(self, *, code: str, historical_anchor_date: date) -> H1CapabilityAuditReport:
        if len(code) != 6 or not code.isdigit():
            raise ValueError("H1 capability probe code is invalid")
        if historical_anchor_date >= H1_SOURCE_CUTOFF:
            raise ValueError("H1 capability probe date exceeds source cutoff")
        failures: list[str] = []
        try:
            tencent = self._source.daily_sample(code, H1_SOURCE_CUTOFF, self._timeout)
            tencent_probe = self._tencent_probe(tencent, H1_SOURCE_CUTOFF)
        except (OSError, TypeError, ValueError):
            tencent_probe = _failed_probe("tencent_qfq_daily")
            failures.append("tencent_qfq_daily_probe_failed")
        try:
            eastmoney = self._source.minute_sample(code, historical_anchor_date, self._timeout)
            eastmoney_probe = self._eastmoney_probe(eastmoney, historical_anchor_date)
        except (OSError, TypeError, ValueError):
            eastmoney_probe = _failed_probe("eastmoney_historical_minute")
            failures.append("eastmoney_historical_minute_probe_failed")
        return BuildH1CapabilityAudit(
            (tencent_probe, eastmoney_probe),
            probe_failures=tuple(failures),
        )

    def _tencent_probe(self, response: HistorySourceSample, cutoff: date) -> H1CapabilityProbe:
        all_rows = response.dates
        rows = tuple(item for item in all_rows if item <= cutoff)
        estimated_requests = math.ceil(5000 * 1600 / max(1, len(all_rows)))
        return H1CapabilityProbe(
            "tencent_qfq_daily",
            min(rows) if rows else None,
            False,
            "qfq",
            False,
            len(all_rows),
            estimated_requests,
            response.response_bytes * 5000,
            estimated_requests * self._timeout,
        )

    def _eastmoney_probe(self, response: HistorySourceSample, cutoff: date) -> H1CapabilityProbe:
        timestamps = response.timestamps
        requested = tuple(item for item in timestamps if item.date() == cutoff)
        times = {item.strftime("%H:%M") for item in requested}
        estimated_requests = 5000 * 1600 if not requested else 5000
        return H1CapabilityProbe(
            "eastmoney_historical_minute",
            cutoff if requested else None,
            "15:00" in times,
            "unsupported",
            False,
            len(requested),
            estimated_requests,
            response.response_bytes * 5000,
            estimated_requests * self._timeout,
        )


class H1CapabilityEvidenceIndex:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "h1_capability_audit.json"

    def write(self, report: H1CapabilityAuditReport) -> H1CapabilityAuditReport:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != report.content_hash:
                raise H1CapabilityEvidenceConflictError("H1 capability artifact identity conflict")
            return existing
        payload = _encode(report)
        if PublishImmutable(self._path, CanonicalJsonText(payload)):
            return self.verify()
        existing = self.verify()
        if existing.content_hash != report.content_hash:
            raise H1CapabilityEvidenceConflictError("H1 capability artifact identity conflict")
        return existing

    def verify(self) -> H1CapabilityAuditReport:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("H1 capability artifact must be an object")
            persisted_hash = raw.pop("content_hash")
            if not isinstance(persisted_hash, str) or CanonicalResearchHash(raw) != persisted_hash:
                raise ValueError("H1 capability artifact hash mismatch")
            report = _decode(cast(dict[str, object], raw))
            if report.content_hash != persisted_hash:
                raise ValueError("H1 capability artifact reconstructed hash mismatch")
            return report
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise H1CapabilityEvidenceConflictError("H1 capability artifact schema or hash is invalid") from exc


def _failed_probe(source: str) -> H1CapabilityProbe:
    return H1CapabilityProbe(source, None, False, "unsupported", False, 0, 0, 0, 0.0)


def _encode(report: H1CapabilityAuditReport) -> dict[str, object]:
    return {
        "probes": [_encode_probe(item) for item in report.probes],
        "strategies": [_encode_strategy(item) for item in report.strategies],
        "schema_version": report.schema_version,
        "production_authority": report.production_authority,
        "probe_failures": list(report.probe_failures),
        "content_hash": report.content_hash,
    }


def _encode_probe(item: H1CapabilityProbe) -> dict[str, object]:
    return {
        "source": item.source,
        "earliest_available": item.earliest_available.isoformat() if item.earliest_available else None,
        "supports_1500": item.supports_1500,
        "adjustment_semantics": item.adjustment_semantics,
        "security_state_effective_at": item.security_state_effective_at,
        "page_size": item.page_size,
        "estimated_requests": item.estimated_requests,
        "estimated_bytes": item.estimated_bytes,
        "estimated_seconds": item.estimated_seconds,
    }


def _encode_strategy(item: H1CapabilityStrategyStatus) -> dict[str, object]:
    return {
        "strategy": item.strategy,
        "state": item.state,
        "failure_reasons": list(item.failure_reasons),
        "terminal_holdout_opened": item.terminal_holdout_opened,
        "production_authority": item.production_authority,
    }


def _decode(raw: dict[str, object]) -> H1CapabilityAuditReport:
    expected = {"probes", "strategies", "schema_version", "production_authority", "probe_failures"}
    if set(raw) != expected:
        raise ValueError("H1 capability artifact schema is invalid")
    probes_raw = raw["probes"]
    strategies_raw = raw["strategies"]
    if not isinstance(probes_raw, list) or not isinstance(strategies_raw, list):
        raise TypeError("H1 capability artifact collections are invalid")
    failures = raw["probe_failures"]
    if not IsTextSequence(failures):
        raise TypeError("H1 capability probe failures are invalid")
    probes = tuple(_decode_probe(item) for item in probes_raw)
    strategies = tuple(_decode_strategy(item) for item in strategies_raw)
    schema = raw["schema_version"]
    authority = raw["production_authority"]
    if not isinstance(schema, str) or not isinstance(authority, bool):
        raise TypeError("H1 capability artifact metadata is invalid")
    return H1CapabilityAuditReport(
        probes=probes,
        strategies=strategies,
        schema_version=schema,
        production_authority=authority,
        probe_failures=tuple(failures),
    )


def _decode_probe(raw: object) -> H1CapabilityProbe:
    if not isinstance(raw, dict):
        raise TypeError("H1 capability probe is invalid")
    expected = {
        "source",
        "earliest_available",
        "supports_1500",
        "adjustment_semantics",
        "security_state_effective_at",
        "page_size",
        "estimated_requests",
        "estimated_bytes",
        "estimated_seconds",
    }
    if set(raw) != expected:
        raise ValueError("H1 capability probe schema is invalid")
    earliest = raw["earliest_available"]
    return H1CapabilityProbe(
        _string(raw["source"]),
        date.fromisoformat(_string(earliest)) if earliest is not None else None,
        _bool(raw["supports_1500"]),
        _string(raw["adjustment_semantics"]),
        _bool(raw["security_state_effective_at"]),
        _int(raw["page_size"]),
        _int(raw["estimated_requests"]),
        _int(raw["estimated_bytes"]),
        float(raw["estimated_seconds"]) if isinstance(raw["estimated_seconds"], (int, float)) else _bad(),
    )


def _decode_strategy(raw: object) -> H1CapabilityStrategyStatus:
    if not isinstance(raw, dict):
        raise TypeError("H1 capability strategy status is invalid")
    expected = {"strategy", "state", "failure_reasons", "terminal_holdout_opened", "production_authority"}
    if set(raw) != expected:
        raise ValueError("H1 capability strategy schema is invalid")
    reasons = raw["failure_reasons"]
    if not IsTextSequence(reasons):
        raise TypeError("H1 capability strategy reasons are invalid")
    return H1CapabilityStrategyStatus(
        cast(ResearchStrategy, _string(raw["strategy"])),
        cast(H1CoverageState, _string(raw["state"])),
        tuple(reasons),
        _bool(raw["terminal_holdout_opened"]),
        _bool(raw["production_authority"]),
    )


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("H1 capability field must be a string")
    return value


def _bool(value: object) -> bool:
    if not IsBoolean(value):
        raise TypeError("H1 capability field must be boolean")
    return value


def _int(value: object) -> int:
    if not IsInteger(value):
        raise TypeError("H1 capability field must be integer")
    return value


def _bad() -> float:
    raise TypeError("H1 capability field must be numeric")


__all__ = [
    "FreeSourceH1CapabilityProbe",
    "H1CapabilityEvidenceConflictError",
    "H1CapabilityEvidenceIndex",
]
