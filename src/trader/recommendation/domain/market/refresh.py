"""Immutable structured-research refresh result."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ResearchRefreshResult:
    requested_codes: tuple[str, ...] = ()
    completed_codes: tuple[str, ...] = ()
    changed_codes: tuple[str, ...] = ()
    partial_codes: tuple[str, ...] = ()
    failed_codes: tuple[str, ...] = ()
    deferred_codes: tuple[str, ...] = ()
    covered_codes: tuple[str, ...] = ()
    data_version: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deadline_reached: bool = False

    def __post_init__(self) -> None:
        groups = (
            self.requested_codes,
            self.completed_codes,
            self.changed_codes,
            self.partial_codes,
            self.failed_codes,
            self.deferred_codes,
            self.covered_codes,
        )
        if any(
            len(group) != len(set(group))
            or any(len(code) != 6 or not code.isdigit() for code in group)
            for group in groups
        ):
            raise ValueError("research refresh codes must be unique normalized six-digit codes")
        requested = set(self.requested_codes)
        if any(not set(group) <= requested for group in groups[1:]):
            raise ValueError("research refresh outcomes must be subsets of requested codes")
        completed = set(self.completed_codes)
        failed = set(self.failed_codes)
        deferred = set(self.deferred_codes)
        if completed & failed or completed & deferred or failed & deferred:
            raise ValueError("research refresh terminal outcome groups must not overlap")
        if not set(self.changed_codes) <= completed:
            raise ValueError("research refresh changed codes must be completed")
        if not set(self.partial_codes) <= completed or not set(self.covered_codes) <= completed:
            raise ValueError("research refresh coverage groups must be completed")
        if set(self.partial_codes) & set(self.covered_codes):
            raise ValueError("research refresh partial and covered groups must not overlap")
        if (self.started_at is None) != (self.completed_at is None):
            raise ValueError("research refresh timestamps must be provided together")
        for value in (self.started_at, self.completed_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("research refresh timestamps must be timezone-aware")
        if self.started_at is not None and self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("research refresh completion cannot precede start")


__all__ = ["ResearchRefreshResult"]
