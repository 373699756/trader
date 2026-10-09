"""Read a small supplier tail without changing the published archive."""

from dataclasses import dataclass
from typing import Protocol

from trader.download.domain.history_tail import HistoryTailRequest, HistoryTailWindow


class HistoryTailSupplierPort(Protocol):
    def fetch(self, request: HistoryTailRequest, *, deadline: float) -> HistoryTailWindow: ...


@dataclass(frozen=True, slots=True)
class FetchHistoryTailUseCase:
    supplier: HistoryTailSupplierPort

    def fetch(self, request: HistoryTailRequest, *, deadline: float) -> HistoryTailWindow:
        result = self.supplier.fetch(request, deadline=deadline)
        if result.code != request.code or tuple(cell.trade_date for cell in result.cells) != request.dates:
            raise ValueError("history_tail_response_incomplete")
        return result
