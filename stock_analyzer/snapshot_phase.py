from __future__ import annotations

from datetime import datetime
from typing import Dict


PRECLOSE_TRADEABLE = "preclose_tradeable"
CLOSE_FALLBACK = "close_fallback"
LEGACY_UNKNOWN = "legacy_unknown"

SNAPSHOT_PHASES = {
    PRECLOSE_TRADEABLE,
    CLOSE_FALLBACK,
    LEGACY_UNKNOWN,
}


def normalize_snapshot_phase(value: object, default: str = LEGACY_UNKNOWN) -> str:
    phase = str(value or "").strip().lower()
    aliases = {
        "preclose": PRECLOSE_TRADEABLE,
        "intraday": PRECLOSE_TRADEABLE,
        "frozen": PRECLOSE_TRADEABLE,
        "close": CLOSE_FALLBACK,
        "close_final": CLOSE_FALLBACK,
        "after_close": CLOSE_FALLBACK,
        "legacy": LEGACY_UNKNOWN,
        "unknown": LEGACY_UNKNOWN,
    }
    phase = aliases.get(phase, phase)
    if phase in SNAPSHOT_PHASES:
        return phase
    fallback = str(default or LEGACY_UNKNOWN).strip().lower()
    return fallback if fallback in SNAPSHOT_PHASES else LEGACY_UNKNOWN


def market_close_reached(now: datetime | None = None, close_time: str = "15:00") -> bool:
    observed = now or datetime.now()
    if observed.weekday() >= 5:
        return False
    close_hour, close_minute = _clock_parts(close_time, 15, 0)
    return (observed.hour, observed.minute) >= (close_hour, close_minute)


def close_quote_is_valid(
    value: object,
    *,
    signal_date: str,
    close_time: str = "15:00",
) -> bool:
    timestamp = _parse_timestamp(value)
    expected_date = str(signal_date or "")[:10]
    if timestamp is None or not expected_date or timestamp.date().isoformat() != expected_date:
        return False
    close_hour, close_minute = _clock_parts(close_time, 15, 0)
    return (timestamp.hour, timestamp.minute) >= (close_hour, close_minute)


def phase_payload(phase: object, *, as_of: str = "") -> Dict[str, object]:
    normalized = normalize_snapshot_phase(phase)
    return {
        "snapshot_phase": normalized,
        "as_of": str(as_of or ""),
        "price_basis": (
            "official_close" if normalized == CLOSE_FALLBACK else
            "signal_time_quote" if normalized == PRECLOSE_TRADEABLE else
            "legacy_unknown"
        ),
        "execution_basis": (
            "theoretical_close_research" if normalized == CLOSE_FALLBACK else
            "tradeable_preclose_reference" if normalized == PRECLOSE_TRADEABLE else
            "legacy_unknown"
        ),
    }


def _clock_parts(value: object, fallback_hour: int, fallback_minute: int):
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text[:5].split(":", 1)
        return int(hour_text), int(minute_text)
    except (TypeError, ValueError):
        return fallback_hour, fallback_minute


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
