"""Append-only artifacts for preregistered Tomorrow shadow evidence."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Literal, cast

from trader.application.research.preregistered_shadow_models import (
    PreregisteredShadowDayRecord,
    PreregisteredShadowGateReport,
    PreregisteredShadowPair,
    ShadowDayStatus,
    ShadowEvidencePhase,
    preregistered_shadow_evidence_manifest_hash,
)
from trader.application.research.replay_models import canonical_hash, canonical_value
from trader.domain.research.historical import ResearchBoard
from trader.domain.research.tomorrow_shadow_preregistration import (
    TOMORROW_SHADOW_CHALLENGER_FAMILY,
    TomorrowShadowCalendarAttestation,
    TomorrowShadowChallengerId,
    TomorrowShadowPreregistration,
)


class PreregisteredShadowArtifactConflictError(RuntimeError):
    pass


class PreregisteredShadowArtifactStore:
    def __init__(self, root: Path, spec: TomorrowShadowPreregistration) -> None:
        self._root = root
        self._spec = spec

    def seal_calendar(self, attestation: TomorrowShadowCalendarAttestation) -> TomorrowShadowCalendarAttestation:
        if attestation.research_spec_hash != self._spec.content_hash:
            raise ValueError("calendar attestation does not match the artifact store spec")
        path = self._identity_root / "calendar-attestation.json"
        payload = _payload(attestation)
        _write_once(path, payload, "calendar attestation")
        return self._read_calendar()

    def append(self, record: PreregisteredShadowDayRecord) -> PreregisteredShadowDayRecord:
        if record.research_spec_hash != self._spec.content_hash:
            raise ValueError("shadow evidence does not match the artifact store spec")
        calendar = self._read_calendar()
        if record.calendar_attestation_hash != calendar.content_hash:
            raise ValueError("shadow evidence calendar attestation does not match")
        if record.phase == "forward":
            historical_path = self._identity_root / "reports" / "historical.json"
            if not historical_path.exists():
                raise ValueError("historical shadow gate report must be sealed before forward evidence")
            historical_raw, historical_hash = _read_payload(historical_path, "historical shadow gate report")
            if record.historical_gate_hash != historical_hash:
                raise ValueError("forward evidence historical shadow gate does not match")
            if not _historical_challenger_passed(historical_raw, record.challenger_id):
                raise ValueError("forward evidence requires a historically passed challenger")
        existing = self.read(record.challenger_id, record.phase, record.planned_trade_date)
        if existing is not None:
            if existing.content_hash != record.content_hash:
                raise PreregisteredShadowArtifactConflictError("shadow evidence identity conflict")
            return existing
        _write_once(
            self._record_path(record.challenger_id, record.phase, record.planned_trade_date),
            _payload(record),
            "shadow evidence",
        )
        stored = self.read(record.challenger_id, record.phase, record.planned_trade_date)
        if stored is None:
            raise PreregisteredShadowArtifactConflictError("shadow evidence was not durably created")
        return stored

    def seal_report(self, report: PreregisteredShadowGateReport) -> PreregisteredShadowGateReport:
        if report.research_spec_hash != self._spec.content_hash:
            raise ValueError("shadow gate report does not match the artifact store spec")
        if report.state == "collecting":
            raise ValueError("collecting shadow gate reports cannot be sealed")
        calendar = self._read_calendar()
        if report.calendar_attestation_hash != calendar.content_hash:
            raise ValueError("shadow gate report calendar attestation does not match")
        if report.scope != "historical":
            historical_path = self._identity_root / "reports" / "historical.json"
            if not historical_path.exists():
                raise ValueError("historical shadow gate report must be sealed before downstream reports")
            _historical_raw, historical_hash = _read_payload(historical_path, "historical shadow gate report")
            if report.historical_report_hash != historical_hash:
                raise ValueError("downstream shadow gate report historical parent does not match")
        records = self._stored_records(report.scope)
        if report.evidence_manifest_hash != preregistered_shadow_evidence_manifest_hash(records):
            raise ValueError("shadow gate report evidence manifest does not match stored records")
        path = self._identity_root / "reports" / f"{report.scope}.json"
        _write_once(path, _payload(report), "shadow gate report")
        return report

    def read(
        self,
        challenger_id: str,
        phase: str,
        trade_date: date,
    ) -> PreregisteredShadowDayRecord | None:
        _validate_key(challenger_id, phase, trade_date, self._spec)
        path = self._record_path(challenger_id, phase, trade_date)
        if not path.exists():
            return None
        raw, stored_hash = _read_payload(path, "shadow evidence")
        try:
            record = _record_from_payload(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise PreregisteredShadowArtifactConflictError("shadow evidence hash or schema is invalid") from exc
        if record.content_hash != stored_hash:
            raise PreregisteredShadowArtifactConflictError("shadow evidence reconstructed hash is invalid")
        return record

    @property
    def _identity_root(self) -> Path:
        return self._root / self._spec.research_identity

    def _record_path(self, challenger_id: str, phase: str, trade_date: date) -> Path:
        return self._identity_root / challenger_id / phase / f"{trade_date.isoformat()}.json"

    def _stored_records(self, scope: str) -> tuple[PreregisteredShadowDayRecord, ...]:
        phases = (
            ("historical",)
            if scope == "historical"
            else (("forward",) if scope == "forward" else ("historical", "forward"))
        )
        records: list[PreregisteredShadowDayRecord] = []
        for challenger_id in TOMORROW_SHADOW_CHALLENGER_FAMILY:
            for phase in phases:
                dates = self._spec.historical_dates if phase == "historical" else self._spec.forward_dates
                for trade_date in dates:
                    record = self.read(challenger_id, phase, trade_date)
                    if record is not None:
                        records.append(record)
        return tuple(records)

    def _read_calendar(self) -> TomorrowShadowCalendarAttestation:
        path = self._identity_root / "calendar-attestation.json"
        if not path.exists():
            raise ValueError("calendar attestation must be sealed before shadow evidence")
        raw, stored_hash = _read_payload(path, "calendar attestation")
        try:
            dates_raw = raw["trading_dates"]
            if not isinstance(dates_raw, list):
                raise TypeError("calendar trading dates are invalid")
            attestation = TomorrowShadowCalendarAttestation(
                research_spec_hash=_string(raw["research_spec_hash"]),
                confirmed_on=date.fromisoformat(_string(raw["confirmed_on"])),
                authority_document_hash=_string(raw["authority_document_hash"]),
                trading_dates=tuple(date.fromisoformat(_string(item)) for item in dates_raw),
                authority=cast(Literal["shanghai_stock_exchange"], _string(raw["authority"])),
                schema_version=_string(raw["schema_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PreregisteredShadowArtifactConflictError("calendar attestation hash or schema is invalid") from exc
        if attestation.content_hash != stored_hash:
            raise PreregisteredShadowArtifactConflictError("calendar attestation reconstructed hash is invalid")
        return attestation


def _payload(
    value: TomorrowShadowCalendarAttestation | PreregisteredShadowDayRecord | PreregisteredShadowGateReport,
) -> dict[str, object]:
    payload = canonical_value(value)
    if not isinstance(payload, dict):
        raise TypeError("preregistered shadow artifact payload must be an object")
    result = cast(dict[str, object], payload)
    result["content_hash"] = value.content_hash
    return result


def _write_once(path: Path, payload: dict[str, object], label: str) -> None:
    if path.exists():
        raw, stored_hash = _read_payload(path, label)
        expected_hash = payload["content_hash"]
        if stored_hash != expected_hash or raw != {
            key: value for key, value in payload.items() if key != "content_hash"
        }:
            raise PreregisteredShadowArtifactConflictError(f"{label} identity conflict")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            _write_once(path, payload, label)
    finally:
        temporary.unlink(missing_ok=True)


def _read_payload(path: Path, label: str) -> tuple[dict[str, object], str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TypeError("artifact is not an object")
        stored_hash = value.pop("content_hash")
        if not isinstance(stored_hash, str) or canonical_hash(value) != stored_hash:
            raise ValueError("artifact hash mismatch")
        return cast(dict[str, object], value), stored_hash
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise PreregisteredShadowArtifactConflictError(f"{label} hash or schema is invalid") from exc


def _record_from_payload(raw: dict[str, object]) -> PreregisteredShadowDayRecord:
    pairs_raw = raw["pairs"]
    if not isinstance(pairs_raw, list):
        raise TypeError("shadow evidence pairs are invalid")
    return PreregisteredShadowDayRecord(
        research_spec_hash=_string(raw["research_spec_hash"]),
        calendar_attestation_hash=_string(raw["calendar_attestation_hash"]),
        historical_gate_hash=_optional_string(raw["historical_gate_hash"]),
        challenger_id=cast(TomorrowShadowChallengerId, _string(raw["challenger_id"])),
        phase=cast(ShadowEvidencePhase, _string(raw["phase"])),
        planned_trade_date=date.fromisoformat(_string(raw["planned_trade_date"])),
        status=cast(ShadowDayStatus, _string(raw["status"])),
        feature_batch_hash=_optional_string(raw["feature_batch_hash"]),
        shadow_report_hash=_optional_string(raw["shadow_report_hash"]),
        selection_report_hash=_optional_string(raw["selection_report_hash"]),
        pairs=tuple(_pair_from_payload(_object(item)) for item in pairs_raw),
        failure_reason=_optional_string(raw["failure_reason"]),
        schema_version=_string(raw["schema_version"]),
    )


def _historical_challenger_passed(raw: dict[str, object], challenger_id: str) -> bool:
    if raw.get("scope") != "historical" or raw.get("state") != "historical_passed":
        raise PreregisteredShadowArtifactConflictError("historical shadow gate report schema is invalid")
    variants = raw.get("variants")
    if not isinstance(variants, list):
        raise PreregisteredShadowArtifactConflictError("historical shadow gate report schema is invalid")
    matches = tuple(item for item in variants if isinstance(item, dict) and item.get("challenger_id") == challenger_id)
    state = matches[0].get("state") if len(matches) == 1 else None
    if not isinstance(state, str) or state not in {"passed", "collecting", "rejected"}:
        raise PreregisteredShadowArtifactConflictError("historical shadow gate report schema is invalid")
    return state == "passed"


def _pair_from_payload(raw: dict[str, object]) -> PreregisteredShadowPair:
    return PreregisteredShadowPair(
        code=_string(raw["code"]),
        board=cast(ResearchBoard, _string(raw["board"])),
        baseline_weight=_float(raw["baseline_weight"]),
        challenger_weight=_float(raw["challenger_weight"]),
        hybrid_weight=_float(raw["hybrid_weight"]),
        gross_excess_return=_float(raw["gross_excess_return"]),
        turnover=_float(raw["turnover"]),
        mae_atr20=_float(raw["mae_atr20"]),
        score=_float(raw["score"]),
        oracle_member=_bool(raw["oracle_member"]),
    )


def _validate_key(challenger_id: str, phase: str, trade_date: date, spec: TomorrowShadowPreregistration) -> None:
    if challenger_id not in TOMORROW_SHADOW_CHALLENGER_FAMILY:
        raise ValueError("unknown preregistered shadow challenger")
    if phase not in {"historical", "forward"}:
        raise ValueError("unknown preregistered shadow phase")
    expected = spec.historical_dates if phase == "historical" else spec.forward_dates
    if trade_date not in expected:
        raise ValueError("preregistered shadow date is outside its fixed window")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("shadow evidence object is invalid")
    return cast(dict[str, object], value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("shadow evidence string value is invalid")
    return value


def _float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("shadow evidence numeric value is invalid")
    return float(value)


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("shadow evidence boolean value is invalid")
    return value


__all__ = [
    "PreregisteredShadowArtifactConflictError",
    "PreregisteredShadowArtifactStore",
]
