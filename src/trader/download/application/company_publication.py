"""Publish fixed local company candidates through explicit one-way checkpoints."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class CompanyPublicationConflict(RuntimeError):
    """The pinned source or formal parent changed before an atomic commit."""


class CompanyPublicationSession(Protocol):
    @property
    def codes(self) -> tuple[str, ...]: ...
    def commit(self) -> tuple[str, ...]: ...
    def confirm(self) -> tuple[str, ...]: ...


class CompanyPublicationPort(Protocol):
    def publication(
        self, codes: Sequence[str], observed_at: datetime, *, changed_only: bool = False,
    ) -> AbstractContextManager[CompanyPublicationSession | None]: ...
    def release_publication_material(self) -> None: ...
    def finish(self, observed_at: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class CompanyPublicationOutcome:
    selected_codes: tuple[str, ...]
    changed_codes: tuple[str, ...]
    pending_codes: tuple[str, ...]


def PublishCompanyEvidence(
    index: CompanyPublicationPort,
    codes: Sequence[str],
    observed_at: datetime,
    *,
    changed_only: bool = False,
) -> CompanyPublicationOutcome:
    """Give each selected code at most one local follow-up; preserve further dirty work."""
    queued = tuple(sorted(set(codes)))
    primary: tuple[str, ...] = ()
    followed: set[str] = set()
    changed: set[str] = set()
    pending: set[str] = set()
    first = True
    while queued:
        with index.publication(queued, observed_at, changed_only=changed_only or not first) as session:
            if session is None:
                break
            if first:
                primary = session.codes
            changed.update(session.commit())
            next_codes = set(session.confirm())
        index.release_publication_material()
        allowed = set(primary)
        pending.update(next_codes - allowed | next_codes & followed)
        queued = tuple(sorted(next_codes & allowed - followed))
        followed.update(queued)
        first = False
    if primary and not changed_only:
        index.finish(observed_at)
    return CompanyPublicationOutcome(primary, tuple(sorted(changed)), tuple(sorted(pending)))


__all__ = [
    "CompanyPublicationSession",
    "CompanyPublicationConflict",
    "CompanyPublicationPort",
    "CompanyPublicationOutcome",
    "PublishCompanyEvidence",
]
