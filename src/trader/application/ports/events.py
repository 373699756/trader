"""In-process event state port."""

from typing import Protocol

from trader.application.events import EventAuditRecord, EventStatus


class EventAuditPort(Protocol):
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
