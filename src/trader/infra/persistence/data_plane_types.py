"""Type and schema metadata shared by the V2 data-plane repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from trader.application.ports.data_plane import (
    HistoricalFeatureRecord,
    RiskEvidenceRecord,
    SecurityMasterRecord,
    SourceCursorRecord,
)

Mode: TypeAlias = Literal["recent", "formal"]


Record: TypeAlias = SecurityMasterRecord | HistoricalFeatureRecord | RiskEvidenceRecord | SourceCursorRecord


_MAX_PAYLOAD_BYTES: Final[int] = 8 * 1024 * 1024
_DEFAULT_SCHEMA_VERSION: Final[str] = "v2_data_plane_v1"


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
    "historical_feature": _Profile(
        family="historical_feature",
        recent_table="historical_feature_recent",
        formal_table="historical_feature_formal",
        identity_fields=("code", "trade_date"),
    ),
    "risk_evidence": _Profile(
        family="risk_evidence",
        recent_table="risk_evidence_recent",
        formal_table="risk_evidence_formal",
        identity_fields=("code", "evidence_id"),
    ),
    "source_cursor": _Profile(
        family="source_cursor",
        recent_table="source_cursor_recent",
        formal_table="source_cursor_formal",
        identity_fields=("cursor_name",),
    ),
}


def profiles() -> dict[str, _Profile]:
    """Return the profile registry."""

    return _PROFILES
