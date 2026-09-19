"""Typed read-only status for the active monthly history archive."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

HistoryArchiveState = Literal["active", "unavailable", "invalid"]
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REASON = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True)
class HistoryArchiveStatus:
    state: HistoryArchiveState
    active_snapshot_hash: str | None
    data_cutoff: date | None
    label_cutoff: date | None
    calendar_sessions: int
    universe_count: int
    partition_count: int
    reason: str | None
    production_authority: bool = False
    point_in_time_parity: bool = False

    def __post_init__(self) -> None:
        counts = (self.calendar_sessions, self.universe_count, self.partition_count)
        valid_hash = self.active_snapshot_hash is None or _SHA256.fullmatch(self.active_snapshot_hash) is not None
        valid_reason = self.reason is None or _REASON.fullmatch(self.reason) is not None
        active_valid = (
            self.state == "active"
            and self.active_snapshot_hash is not None
            and self.data_cutoff is not None
            and self.label_cutoff is not None
            and self.calendar_sessions > 0
            and self.universe_count > 0
            and self.partition_count > 0
            and self.reason is None
        )
        unavailable_valid = (
            self.state == "unavailable"
            and self.active_snapshot_hash is None
            and self.data_cutoff is None
            and self.label_cutoff is None
            and counts == (0, 0, 0)
            and self.reason is not None
        )
        invalid_valid = self.state == "invalid" and self.reason is not None
        if (
            any(type(value) is not int or value < 0 for value in counts)
            or not valid_hash
            or not valid_reason
            or self.production_authority
            or self.point_in_time_parity
            or not (active_valid or unavailable_valid or invalid_valid)
        ):
            raise ValueError("history archive status is invalid")


__all__ = ["HistoryArchiveState", "HistoryArchiveStatus"]
