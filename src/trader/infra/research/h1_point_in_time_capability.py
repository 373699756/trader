"""Read-only supplier capability probes and immutable H1 audit storage."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Protocol, cast

from trader.domain.research.h1_point_in_time import (
    H1_SOURCE_CUTOFF,
    H1CapabilityAuditReport,
    H1CapabilityProbe,
    H1CapabilityStrategyStatus,
    H1CoverageState,
    H1Strategy,
    build_h1_capability_audit,
    canonical_hash,
)


class _Response(Protocol):
    content: bytes

    def json(self) -> object: ...

    def raise_for_status(self) -> None: ...


class H1HTTPSession(Protocol):
    def get(self, url: str, *, params: dict[str, object], timeout: float) -> _Response: ...


class H1CapabilityArtifactConflictError(RuntimeError):
    """Raised when a capability artifact is missing, tampered, or conflicting."""


class FreeSourceH1CapabilityProbe:
    """Probe only bounded metadata; supplier payload values are never retained."""

    def __init__(self, session: H1HTTPSession, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("H1 capability timeout must be positive")
        self._session = session
        self._timeout = timeout_seconds

    def run(self, *, code: str, historical_anchor_date: date) -> H1CapabilityAuditReport:
        if len(code) != 6 or not code.isdigit():
            raise ValueError("H1 capability probe code is invalid")
        if historical_anchor_date >= H1_SOURCE_CUTOFF:
            raise ValueError("H1 capability probe date exceeds source cutoff")
        tencent = self._request(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            {
                "_var": f"kline_dayqfq{H1_SOURCE_CUTOFF.year}",
                "param": (f"{_symbol(code)},day,2015-01-01,{H1_SOURCE_CUTOFF.isoformat()},1600,qfq"),
            },
        )
        eastmoney = self._request(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": _secid(code),
                "klt": "1",
                "fqt": "0",
                "beg": historical_anchor_date.strftime("%Y%m%d"),
                "end": historical_anchor_date.strftime("%Y%m%d"),
                "lmt": "1000",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
            },
        )
        return build_h1_capability_audit(
            (
                self._tencent_probe(tencent, H1_SOURCE_CUTOFF),
                self._eastmoney_probe(eastmoney, historical_anchor_date),
            )
        )

    def _request(self, url: str, params: dict[str, object]) -> _Response:
        response = self._session.get(url, params=params, timeout=self._timeout)
        response.raise_for_status()
        return response

    def _tencent_probe(self, response: _Response, cutoff: date) -> H1CapabilityProbe:
        all_rows = _date_rows(_json_payload(response), "data", "qfqday", date.max, nested=True)
        rows = tuple(item for item in all_rows if item <= cutoff)
        estimated_requests = math.ceil(5000 * 1600 / max(1, len(all_rows)))
        return H1CapabilityProbe(
            "tencent_qfq_daily",
            min(rows) if rows else None,
            False,
            False,
            "qfq",
            False,
            len(all_rows),
            estimated_requests,
            len(response.content) * 5000,
            estimated_requests * self._timeout,
        )

    def _eastmoney_probe(self, response: _Response, cutoff: date) -> H1CapabilityProbe:
        timestamps = _timestamp_rows(_json_payload(response), "data", "klines")
        requested = tuple(item for item in timestamps if item.startswith(cutoff.isoformat()))
        times = {item[11:16] for item in requested if len(item) >= 16}
        estimated_requests = 5000 * 1600 if not requested else 5000
        return H1CapabilityProbe(
            "eastmoney_historical_minute",
            cutoff if requested else None,
            "11:20" in times,
            "14:50" in times,
            "unsupported",
            False,
            len(requested),
            estimated_requests,
            len(response.content) * 5000,
            estimated_requests * self._timeout,
        )


class H1CapabilityArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._path = root / "h1_capability_audit.json"

    def write(self, report: H1CapabilityAuditReport) -> H1CapabilityAuditReport:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            existing = self.verify()
            if existing.content_hash != report.content_hash:
                raise H1CapabilityArtifactConflictError("H1 capability artifact identity conflict")
            return existing
        payload = _encode(report)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self._path.name}.", suffix=".tmp", dir=self._root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                existing = self.verify()
                if existing.content_hash != report.content_hash:
                    raise H1CapabilityArtifactConflictError("H1 capability artifact identity conflict") from None
                return existing
        finally:
            temporary.unlink(missing_ok=True)
        return self.verify()

    def verify(self) -> H1CapabilityAuditReport:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("H1 capability artifact must be an object")
            stored_hash = raw.pop("content_hash")
            if not isinstance(stored_hash, str) or canonical_hash(raw) != stored_hash:
                raise ValueError("H1 capability artifact hash mismatch")
            report = _decode(cast(dict[str, object], raw))
            if report.content_hash != stored_hash:
                raise ValueError("H1 capability artifact reconstructed hash mismatch")
            return report
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise H1CapabilityArtifactConflictError("H1 capability artifact schema or hash is invalid") from exc


def _json_payload(response: _Response) -> object:
    try:
        return response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("H1 capability supplier payload is invalid") from exc


def _date_rows(payload: object, parent: str, child: str, cutoff: date, *, nested: bool) -> tuple[date, ...]:
    if not isinstance(payload, dict):
        return ()
    value = payload.get(parent)
    if not isinstance(value, dict):
        return ()
    if nested:
        values: list[object] = []
        for item in value.values():
            if isinstance(item, dict) and isinstance(item.get(child), list):
                values.extend(cast(list[object], item[child]))
    else:
        values = value.get(child, []) if isinstance(value.get(child), list) else []
    dates: list[date] = []
    for item in values:
        raw = item[0] if isinstance(item, list) and item else item
        if not isinstance(raw, str):
            continue
        try:
            parsed = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if parsed <= cutoff:
            dates.append(parsed)
    return tuple(dates)


def _timestamp_rows(payload: object, parent: str, child: str) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    value = payload.get(parent)
    if not isinstance(value, dict):
        return ()
    rows = value.get(child)
    if not isinstance(rows, list):
        return ()
    return tuple(item.split(",", 1)[0] for item in rows if isinstance(item, str))


def _symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def _encode(report: H1CapabilityAuditReport) -> dict[str, object]:
    return {
        "probes": [_encode_probe(item) for item in report.probes],
        "strategies": [_encode_strategy(item) for item in report.strategies],
        "schema_version": report.schema_version,
        "production_authority": report.production_authority,
        "content_hash": report.content_hash,
    }


def _encode_probe(item: H1CapabilityProbe) -> dict[str, object]:
    return {
        "source": item.source,
        "earliest_available": item.earliest_available.isoformat() if item.earliest_available else None,
        "supports_today_1120": item.supports_today_1120,
        "supports_1450": item.supports_1450,
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
    expected = {"probes", "strategies", "schema_version", "production_authority"}
    if set(raw) != expected:
        raise ValueError("H1 capability artifact schema is invalid")
    probes_raw = raw["probes"]
    strategies_raw = raw["strategies"]
    if not isinstance(probes_raw, list) or not isinstance(strategies_raw, list):
        raise TypeError("H1 capability artifact collections are invalid")
    probes = tuple(_decode_probe(item) for item in probes_raw)
    strategies = tuple(_decode_strategy(item) for item in strategies_raw)
    schema = raw["schema_version"]
    authority = raw["production_authority"]
    if not isinstance(schema, str) or not isinstance(authority, bool):
        raise TypeError("H1 capability artifact metadata is invalid")
    return H1CapabilityAuditReport(probes, strategies, schema, authority)


def _decode_probe(raw: object) -> H1CapabilityProbe:
    if not isinstance(raw, dict):
        raise TypeError("H1 capability probe is invalid")
    expected = {
        "source",
        "earliest_available",
        "supports_today_1120",
        "supports_1450",
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
        _bool(raw["supports_today_1120"]),
        _bool(raw["supports_1450"]),
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
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise TypeError("H1 capability strategy reasons are invalid")
    return H1CapabilityStrategyStatus(
        cast(H1Strategy, _string(raw["strategy"])),
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
    if not isinstance(value, bool):
        raise TypeError("H1 capability field must be boolean")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("H1 capability field must be integer")
    return value


def _bad() -> float:
    raise TypeError("H1 capability field must be numeric")


__all__ = [
    "FreeSourceH1CapabilityProbe",
    "H1CapabilityArtifactConflictError",
    "H1CapabilityArtifactStore",
    "H1HTTPSession",
]
