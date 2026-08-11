"""Serialization helpers for persisted trading-calendar state."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date

from trader.application.cache import canonical_json_bytes
from trader.application.ports.data_plane import SourceCursorRecord
from trader.application.ports.types import JsonObject, JsonValue
from trader.infra.market_data.observations import JsonScalar, SourceObservation


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def trading_calendar_cursor_from_observations(
    calendars: Sequence[SourceObservation],
) -> str | None:
    dates: list[date] = []
    for observation in calendars:
        if observation.status != "success":
            continue
        parsed = parse_date(observation.subject_key)
        calendar_date = observation.fields.get("calendar_date")
        if parsed is None and isinstance(calendar_date, str):
            parsed = parse_date(calendar_date)
        if parsed is not None:
            dates.append(parsed)
    if not dates:
        return None
    return max(dates).isoformat()


def calendar_sessions_payload(calendars: Sequence[SourceObservation]) -> tuple[JsonValue, ...]:
    sessions: list[JsonObject] = []
    for observation in calendars:
        if observation.status != "success":
            continue
        raw_date = observation.fields.get("calendar_date")
        calendar_date = raw_date if isinstance(raw_date, str) else observation.subject_key
        if parse_date(calendar_date) is None:
            continue
        session: dict[str, JsonValue] = {
            "calendar_date": calendar_date,
            "exchange": observation.fields.get("exchange")
            if isinstance(observation.fields.get("exchange"), str)
            else "",
            "is_open": observation.fields.get("is_open") is True,
            "pretrade_date": observation.fields.get("pretrade_date")
            if isinstance(observation.fields.get("pretrade_date"), str)
            else "",
        }
        sessions.append(session)
    return tuple(sorted(sessions, key=lambda item: str(item["calendar_date"])))


def calendar_observations_from_record(record: SourceCursorRecord) -> tuple[SourceObservation, ...]:
    raw_sessions = record.payload.get("sessions")
    if not isinstance(raw_sessions, (tuple, list)):
        return ()
    observations: list[SourceObservation] = []
    for raw_session in raw_sessions:
        if not isinstance(raw_session, Mapping):
            continue
        observation = _calendar_observation_from_session(record, raw_session)
        if observation is not None:
            observations.append(observation)
    return tuple(sorted(observations, key=lambda item: item.subject_key))


def _calendar_observation_from_session(
    record: SourceCursorRecord,
    raw_session: Mapping[str, object],
) -> SourceObservation | None:
    raw_date = raw_session.get("calendar_date")
    if not isinstance(raw_date, str) or parse_date(raw_date) is None:
        return None
    raw_exchange = raw_session.get("exchange")
    raw_pretrade_date = raw_session.get("pretrade_date")
    fields: dict[str, JsonScalar] = {
        "calendar_date": raw_date,
        "exchange": raw_exchange if isinstance(raw_exchange, str) else "",
        "is_open": raw_session.get("is_open") is True,
        "pretrade_date": raw_pretrade_date if isinstance(raw_pretrade_date, str) else "",
    }
    return SourceObservation(
        source=record.source,
        subject_key=raw_date,
        observed_at=record.observed_at,
        source_time=record.source_time,
        received_at=record.observed_at,
        effective_at=record.source_time,
        data_version=record.data_version,
        fields=fields,
        missing_reasons={},
        payload_hash=hashlib.sha256(canonical_json_bytes(fields)).hexdigest(),
        status="success",
        error_code=None,
    )


__all__ = [
    "calendar_observations_from_record",
    "calendar_sessions_payload",
    "parse_date",
    "trading_calendar_cursor_from_observations",
]
