"""Type and schema metadata shared by the unified data-plane persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from trader.recommendation.application.ports.data_plane_records import (
    SecurityMasterRecord,
    SourceCursorRecord,
    TradingCalendarRecord,
)

Mode: TypeAlias = Literal["recent", "formal"]


Record: TypeAlias = SecurityMasterRecord | SourceCursorRecord | TradingCalendarRecord


_MAX_PAYLOAD_BYTES: Final[int] = 8 * 1024 * 1024
_DEFAULT_SCHEMA_VERSION: Final[str] = "data_plane"


@dataclass(frozen=True)
class _Profile:
    family: str
    recent_table: str
    formal_table: str
    identity_fields: tuple[str, ...]


_PROFILES: dict[str, _Profile] = {
    "security_master": _Profile(
        family="security_master",
        recent_table="security_master_recent",
        formal_table="security_master_formal",
        identity_fields=("code",),
    ),
    "source_cursor": _Profile(
        family="source_cursor",
        recent_table="source_cursor_recent",
        formal_table="source_cursor_formal",
        identity_fields=("cursor_name",),
    ),
    "trading_calendar": _Profile(
        family="trading_calendar",
        recent_table="trading_calendar_recent",
        formal_table="trading_calendar_formal",
        identity_fields=("calendar_name",),
    ),
}


def Profiles() -> dict[str, _Profile]:
    """Return the profile index."""

    return _PROFILES
