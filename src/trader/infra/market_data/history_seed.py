"""Remote daily-history client composition."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from trader.infra.market_data.history import DailyBar


class DailyHistoryClient(Protocol):
    def fetch_history(self, code: str, *, days: int = 61) -> Sequence[DailyBar]: ...


class FallbackHistoryClient:
    """Prefer Tencent qfq history and fall back to Eastmoney qfq history."""

    def __init__(
        self,
        primary: DailyHistoryClient,
        fallback: DailyHistoryClient,
        *,
        minimum_rows: int = 20,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._minimum_rows = max(1, minimum_rows)

    def fetch_history(self, code: str, *, days: int = 61) -> tuple[DailyBar, ...]:
        primary = _safe_history(self._primary, code, days)
        if len(primary) >= self._minimum_rows:
            return primary[-days:]
        fallback = _safe_history(self._fallback, code, days)
        selected = fallback if len(fallback) >= len(primary) else primary
        return selected[-days:]


def _safe_history(client: DailyHistoryClient, code: str, days: int) -> tuple[DailyBar, ...]:
    try:
        return tuple(client.fetch_history(code, days=days))
    except Exception:
        return ()


__all__ = ["DailyHistoryClient", "FallbackHistoryClient"]
