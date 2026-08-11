"""Canonical row codec and integrity checks for V2 data-plane persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import TypedDict, cast

from trader.application.ports.data_plane import (
    HistoricalFeatureRecord,
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
    TradingCalendarRecord,
)
from trader.application.ports.types import JsonObject
from trader.infra.persistence.data_plane_types import Record, _Profile


class _CommonRecordFields(TypedDict):
    observed_at: datetime
    source_time: datetime
    source: str
    data_version: str
    payload: JsonObject
    payload_hash: str
    schema_version: str


def record_to_row(profile: _Profile, record: Record, *, freeze_id: str | None = None) -> dict[str, object]:
    if profile.family == "security_master":
        base = _security_master_identity(record)
    elif profile.family == "historical_feature":
        base = _historical_feature_identity(record)
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


def _historical_feature_identity(record: Record) -> dict[str, object]:
    if not isinstance(record, HistoricalFeatureRecord):
        raise TypeError("historical feature family requires HistoricalFeatureRecord")
    return {"code": record.code, "trade_date": record.trade_date}


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


def assert_committed_record_integrity(table: str, row: sqlite3.Row) -> None:
    payload = parse_payload_text(text(row["payload"]))
    record_payload_for_table(table, payload)
    payload_text = canonical_json(payload)
    if sha256(payload_text.encode("utf-8")) != text(row["payload_hash"]):
        raise ValueError("payload hash mismatch")
    parse_datetime(text(row["observed_at"]))
    parse_datetime(text(row["source_time"]))


def row_to_record(table: str, row: sqlite3.Row) -> Record:
    payload = _row_payload(table, row)
    common: _CommonRecordFields = {
        "observed_at": parse_datetime(text(row["observed_at"])),
        "source_time": parse_datetime(text(row["source_time"])),
        "source": text(row["source"]),
        "data_version": text(row["data_version"]),
        "payload": payload,
        "payload_hash": text(row["payload_hash"]),
        "schema_version": text(row["schema_version"]),
    }
    if table in {"security_master_recent", "security_master_formal"}:
        return SecurityMasterRecord(code=text(row["code"]), **common)
    if table in {"historical_feature_recent", "historical_feature_formal"}:
        return HistoricalFeatureRecord(
            code=text(row["code"]),
            trade_date=text(row["trade_date"]),
            **common,
        )
    if table in {"risk_evidence_recent", "risk_evidence_formal"}:
        return RiskEvidenceRecord(
            code=text(row["code"]),
            evidence_id=text(row["evidence_id"]),
            **common,
        )
    if table in {"source_cursor_recent", "source_cursor_formal"}:
        return SourceCursorRecord(
            cursor_name=text(row["cursor_name"]),
            cursor_value=text(row["cursor_value"]),
            **common,
        )
    if table in {"trading_calendar_recent", "trading_calendar_formal"}:
        return TradingCalendarRecord(calendar_name=text(row["calendar_name"]), **common)
    raise ValueError(f"unsupported table: {table}")


def record_payload_for_table(table: str, payload: JsonObject) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError("payload root must be an object")
    supported = (
        "security_master_",
        "historical_feature_",
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


def table_to_record_kind(table: str) -> str:
    for prefix, kind in (
        ("security_master_", "security_master"),
        ("historical_feature_", "historical_feature"),
        ("risk_evidence_", "risk_evidence"),
        ("source_cursor_", "source_cursor"),
        ("trading_calendar_", "trading_calendar"),
    ):
        if table.startswith(prefix):
            return kind
    raise ValueError(f"unsupported table: {table}")


def pk_fields_from_table(table: str) -> tuple[str, ...]:
    identities = identity_fields_for_table(table)
    if table.endswith("_formal"):
        return ("freeze_id", *identities)
    if table.endswith("_recent"):
        return identities
    raise ValueError(f"unsupported table: {table}")


def identity_fields_for_table(table: str) -> tuple[str, ...]:
    if table.startswith("security_master_"):
        return ("code",)
    if table.startswith("historical_feature_"):
        return ("code", "trade_date")
    if table.startswith("risk_evidence_"):
        return ("code", "evidence_id")
    if table.startswith("source_cursor_"):
        return ("cursor_name",)
    if table.startswith("trading_calendar_"):
        return ("calendar_name",)
    raise ValueError(f"unsupported table: {table}")


def identity_values_from_row(table: str, row: sqlite3.Row) -> tuple[str, ...]:
    return tuple(text(row[field]) for field in pk_fields_from_table(table))


def to_payload_dict(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    return dict(payload)


def canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return canonical_json(payload).encode("utf-8")


def parse_payload_text(raw: str) -> JsonObject:
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise TypeError("payload root must be an object")
    return cast(JsonObject, decoded)


def parse_payload_bytes(raw: bytes) -> JsonObject:
    return parse_payload_text(raw.decode("utf-8"))


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if value is None:
        raise TypeError("required text field is missing")
    return str(value)


def parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(text(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def iso_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.isoformat()


def _row_payload(table: str, row: sqlite3.Row) -> JsonObject:
    payload = parse_payload_text(text(row["payload"]))
    record_payload_for_table(table, payload)
    return payload


__all__ = [
    "assert_committed_record_integrity",
    "canonical_json",
    "canonical_json_bytes",
    "identity_fields_for_table",
    "identity_values_from_row",
    "iso_datetime",
    "parse_datetime",
    "parse_payload_bytes",
    "pk_fields_from_table",
    "record_payload_for_table",
    "record_to_row",
    "row_to_record",
    "sha256",
    "table_to_record_kind",
    "text",
    "to_payload_dict",
]
