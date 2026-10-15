"""Canonical row codec and integrity checks for unified data-plane persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import TypedDict, cast

from trader.infra.serialization.canonical import CanonicalJsonText
from trader.recommendation.application.ports.data_plane_records import (
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
    TradingCalendarRecord,
)
from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.infra.persistence.data_plane_types import Record, _Profile


class _CommonRecordFields(TypedDict):
    observed_at: datetime
    source_time: datetime
    source: str
    data_version: str
    payload: JsonObject
    payload_hash: str
    schema_version: str


def RecordToRow(profile: _Profile, record: Record, *, freeze_id: str | None = None) -> dict[str, object]:
    if profile.family == "security_master":
        base = _security_master_identity(record)
    elif profile.family == "risk_evidence":
        base = _risk_evidence_identity(record)
    elif profile.family == "source_cursor":
        base = _source_cursor_identity(record)
    elif profile.family == "trading_calendar":
        base = _trading_calendar_identity(record)
    else:
        raise ValueError(f"unknown family: {profile.family}")
    if freeze_id is not None:
        base["freeze_id"] = freeze_id
    return base


def _security_master_identity(record: Record) -> dict[str, object]:
    if not isinstance(record, SecurityMasterRecord):
        raise TypeError("security master family requires SecurityMasterRecord")
    return {"code": record.code}


def _risk_evidence_identity(record: Record) -> dict[str, object]:
    if not isinstance(record, RiskEvidenceRecord):
        raise TypeError("risk evidence family requires RiskEvidenceRecord")
    return {"code": record.code, "evidence_id": record.evidence_id}


def _source_cursor_identity(record: Record) -> dict[str, object]:
    if not isinstance(record, SourceCursorRecord):
        raise TypeError("source cursor family requires SourceCursorRecord")
    return {"cursor_name": record.cursor_name, "cursor_value": record.cursor_value}


def _trading_calendar_identity(record: Record) -> dict[str, object]:
    if not isinstance(record, TradingCalendarRecord):
        raise TypeError("trading calendar family requires TradingCalendarRecord")
    return {"calendar_name": record.calendar_name}


def AssertCommittedRecordIntegrity(table: str, row: sqlite3.Row) -> None:
    payload = ParsePayloadText(Text(row["payload"]))
    RecordPayloadForTable(table, payload)
    payload_text = CanonicalJson(payload)
    if Sha256(payload_text.encode("utf-8")) != Text(row["payload_hash"]):
        raise ValueError("payload hash mismatch")
    ParseDatetime(Text(row["observed_at"]))
    ParseDatetime(Text(row["source_time"]))


def RowToRecord(table: str, row: sqlite3.Row) -> Record:
    payload = _row_payload(table, row)
    common: _CommonRecordFields = {
        "observed_at": ParseDatetime(Text(row["observed_at"])),
        "source_time": ParseDatetime(Text(row["source_time"])),
        "source": Text(row["source"]),
        "data_version": Text(row["data_version"]),
        "payload": payload,
        "payload_hash": Text(row["payload_hash"]),
        "schema_version": Text(row["schema_version"]),
    }
    if table in {"security_master_recent", "security_master_formal"}:
        return SecurityMasterRecord(code=Text(row["code"]), **common)
    if table in {"risk_evidence_recent", "risk_evidence_formal"}:
        return RiskEvidenceRecord(
            code=Text(row["code"]),
            evidence_id=Text(row["evidence_id"]),
            **common,
        )
    if table in {"source_cursor_recent", "source_cursor_formal"}:
        return SourceCursorRecord(
            cursor_name=Text(row["cursor_name"]),
            cursor_value=Text(row["cursor_value"]),
            **common,
        )
    if table in {"trading_calendar_recent", "trading_calendar_formal"}:
        return TradingCalendarRecord(calendar_name=Text(row["calendar_name"]), **common)
    raise ValueError(f"unsupported table: {table}")


def RecordPayloadForTable(table: str, payload: JsonObject) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError("payload root must be an object")
    supported = (
        "security_master_",
        "risk_evidence_",
        "source_cursor_",
        "trading_calendar_",
    )
    if not table.startswith(supported):
        raise ValueError(f"unsupported table: {table}")
    if table.startswith("source_cursor_") and not payload:
        return
    if not payload:
        raise ValueError("persisted data-plane facts must not be empty")


def TableToRecordKind(table: str) -> str:
    for prefix, kind in (
        ("security_master_", "security_master"),
        ("risk_evidence_", "risk_evidence"),
        ("source_cursor_", "source_cursor"),
        ("trading_calendar_", "trading_calendar"),
    ):
        if table.startswith(prefix):
            return kind
    raise ValueError(f"unsupported table: {table}")


def PkFieldsFromTable(table: str) -> tuple[str, ...]:
    identities = IdentityFieldsForTable(table)
    if table.endswith("_formal"):
        return ("freeze_id", *identities)
    if table.endswith("_recent"):
        return identities
    raise ValueError(f"unsupported table: {table}")


def IdentityFieldsForTable(table: str) -> tuple[str, ...]:
    if table.startswith("security_master_"):
        return ("code",)
    if table.startswith("risk_evidence_"):
        return ("code", "evidence_id")
    if table.startswith("source_cursor_"):
        return ("cursor_name",)
    if table.startswith("trading_calendar_"):
        return ("calendar_name",)
    raise ValueError(f"unsupported table: {table}")


def IdentityValuesFromRow(table: str, row: sqlite3.Row) -> tuple[str, ...]:
    return tuple(Text(row[field]) for field in PkFieldsFromTable(table))


def ToPayloadDict(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return {str(key): _thaw_json(value) for key, value in payload.items()}


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_json(item) for item in value]
    return value


def CanonicalJson(payload: Mapping[str, object]) -> str:
    return CanonicalJsonText(payload, ascii_only=False)


def CanonicalJsonBytes(payload: Mapping[str, object]) -> bytes:
    return CanonicalJsonText(payload, ascii_only=False).encode("utf-8")


def ParsePayloadText(raw: str) -> JsonObject:
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise TypeError("payload root must be an object")
    return cast(JsonObject, decoded)


def ParsePayloadBytes(raw: bytes) -> JsonObject:
    return ParsePayloadText(raw.decode("utf-8"))


def Sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def Text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if value is None:
        raise TypeError("required text field is missing")
    return str(value)


def ParseDatetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(Text(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def IsoDatetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat()


def _row_payload(table: str, row: sqlite3.Row) -> JsonObject:
    payload = ParsePayloadText(Text(row["payload"]))
    RecordPayloadForTable(table, payload)
    return payload


__all__ = [
    "AssertCommittedRecordIntegrity",
    "CanonicalJson",
    "CanonicalJsonBytes",
    "IdentityFieldsForTable",
    "IdentityValuesFromRow",
    "IsoDatetime",
    "ParseDatetime",
    "ParsePayloadBytes",
    "PkFieldsFromTable",
    "RecordPayloadForTable",
    "RecordToRow",
    "RowToRecord",
    "Sha256",
    "TableToRecordKind",
    "Text",
    "ToPayloadDict",
]
