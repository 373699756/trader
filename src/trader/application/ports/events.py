"""Event audit read/write ports."""

from collections.abc import Sequence
from typing import Protocol

from trader.application.events import EventAuditRecord, EventStatus


class EventReaderPort(Protocol):
    def list_events(self, *, cursor: int, limit: int) -> Sequence[EventAuditRecord]: ...


class EventAuditPort(EventReaderPort, Protocol):
    def reserve_event(self, event: EventAuditRecord) -> bool: ...

    def compare_and_set_event(
        self,
        event_id: str,
        *,
        expected_status: EventStatus,
        status: EventStatus,
        retry_count: int,
        error: str = "",
    ) -> bool: ...

    def pending_priority_events(self) -> Sequence[EventAuditRecord]: ...
