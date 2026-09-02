"""Sequential BaoStock shard download service owned by the offline data plane."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockDailySpec,
    BaoStockSecurity,
    BaoStockSourceVersions,
)


@dataclass(frozen=True)
class BaoStockShardContext:
    calendar: BaoStockCalendar
    universe: tuple[BaoStockSecurity, ...]
    source_versions: BaoStockSourceVersions


class BaoStockDailyGateway(Protocol):
    def fetch_calendar(self, spec: BaoStockDailySpec) -> BaoStockCalendar: ...

    def fetch_universe(self, spec: BaoStockDailySpec) -> tuple[BaoStockSecurity, ...]: ...

    def fetch_code_batch(
        self,
        spec: BaoStockDailySpec,
        security: BaoStockSecurity,
        calendar: BaoStockCalendar,
    ) -> BaoStockCodeBatch: ...

    def source_versions(self) -> BaoStockSourceVersions: ...


class BaoStockDailyShardPort(Protocol):
    def context(self, spec: BaoStockDailySpec) -> BaoStockShardContext | None: ...

    def initialize(
        self,
        spec: BaoStockDailySpec,
        calendar: BaoStockCalendar,
        universe: tuple[BaoStockSecurity, ...],
        source_versions: BaoStockSourceVersions,
    ) -> None: ...

    def completed_codes(self, spec: BaoStockDailySpec) -> frozenset[str]: ...

    def save_batch(self, spec: BaoStockDailySpec, batch: BaoStockCodeBatch) -> None: ...

    def record_failure(self, spec: BaoStockDailySpec, code: str, error_code: str) -> None: ...


@dataclass(frozen=True)
class BaoStockDailyDownloadResult:
    spec_hash: str
    universe_count: int
    previously_completed: int
    attempted: int
    downloaded: int
    failed: int
    production_authority: bool = False

    def __post_init__(self) -> None:
        values = (
            self.universe_count,
            self.previously_completed,
            self.attempted,
            self.downloaded,
            self.failed,
        )
        if any(value < 0 for value in values) or self.downloaded + self.failed != self.attempted:
            raise ValueError("BaoStock download counts are invalid")
        if self.production_authority:
            raise ValueError("BaoStock download cannot authorize production")


class BaoStockDailyDownloadService:
    """Download one deterministic code shard; process supervision belongs to Codex D."""

    def __init__(self, gateway: BaoStockDailyGateway, archive: BaoStockDailyShardPort) -> None:
        self._gateway = gateway
        self._archive = archive

    def execute(
        self,
        spec: BaoStockDailySpec,
        *,
        codes: tuple[str, ...] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> BaoStockDailyDownloadResult:
        context = self._archive.context(spec)
        if context is None:
            context = self._initialize(spec)
        universe = tuple(sorted(context.universe, key=lambda item: item.code))
        selected = set(codes) if codes is not None else {item.code for item in universe}
        unknown = selected.difference(item.code for item in universe)
        if unknown:
            raise ValueError("BaoStock requested code is outside the registered universe")
        completed = self._archive.completed_codes(spec)
        pending = tuple(item for item in universe if item.code in selected and item.code not in completed)
        downloaded = failed = 0
        for position, security in enumerate(pending, start=1):
            try:
                batch = self._gateway.fetch_code_batch(spec, security, context.calendar)
            except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                failed += 1
                self._archive.record_failure(spec, security.code, _failure_code(exc))
            else:
                self._archive.save_batch(spec, batch)
                downloaded += 1
            if progress is not None:
                progress(position, len(pending), security.code)
        return BaoStockDailyDownloadResult(
            spec_hash=spec.content_hash,
            universe_count=len(universe),
            previously_completed=len(completed.intersection(selected)),
            attempted=len(pending),
            downloaded=downloaded,
            failed=failed,
        )

    def _initialize(self, spec: BaoStockDailySpec) -> BaoStockShardContext:
        calendar = self._gateway.fetch_calendar(spec)
        if len(calendar.open_dates) != spec.sessions:
            raise ValueError("BaoStock calendar session count does not match the requested bound")
        universe = tuple(sorted(self._gateway.fetch_universe(spec), key=lambda item: item.code))
        if not universe or len({item.code for item in universe}) != len(universe):
            raise ValueError("BaoStock universe must be non-empty and unique")
        context = BaoStockShardContext(calendar, universe, self._gateway.source_versions())
        self._archive.initialize(spec, context.calendar, context.universe, context.source_versions)
        return context


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "supplier_call_timeout"
    if isinstance(exc, OSError):
        return "supplier_io_failed"
    if isinstance(exc, RuntimeError):
        return "supplier_query_failed"
    return "supplier_payload_invalid"


__all__ = [
    "BaoStockDailyDownloadResult",
    "BaoStockDailyDownloadService",
    "BaoStockDailyGateway",
    "BaoStockDailyShardPort",
    "BaoStockShardContext",
]
