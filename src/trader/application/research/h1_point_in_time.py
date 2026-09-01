"""Bounded H1 point-in-time archive orchestration and coverage audit."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.domain.research.h1_point_in_time import (
    H1CapabilityProbe,
    H1CoverageAudit,
    H1PointInTimeRecord,
    H1PointInTimeSpec,
    H1Strategy,
)
from trader.application.research.historical_screening import HistoricalSecurity


class H1PointInTimeProvider(Protocol):
    def fetch(self, code: str, spec: H1PointInTimeSpec) -> Sequence[H1PointInTimeRecord]: ...


class H1UniverseProvider(Protocol):
    def fetch(self) -> Sequence[HistoricalSecurity]: ...


class H1ArchivePort(Protocol):
    def registered_universe(self, strategy: H1Strategy) -> tuple[HistoricalSecurity, ...]: ...

    def register_universe(self, spec: H1PointInTimeSpec, universe: Sequence[HistoricalSecurity]) -> None: ...

    def completed_codes(self, strategy: H1Strategy) -> frozenset[str]: ...

    def save_records(self, spec: H1PointInTimeSpec, code: str, records: Sequence[H1PointInTimeRecord]) -> None: ...

    def record_failure(self, spec: H1PointInTimeSpec, code: str, error_code: str) -> None: ...

    def audit(self, spec: H1PointInTimeSpec) -> H1CoverageAudit: ...


@dataclass(frozen=True)
class H1DownloadResult:
    strategy: H1Strategy
    universe_count: int
    previously_completed: int
    attempted: int
    downloaded: int
    failed: int


class H1CapabilityProbePort(Protocol):
    def probe(self, *, source_cutoff: date, max_history_sessions: int) -> H1CapabilityProbe: ...


def run_capability_probe(
    provider: H1CapabilityProbePort,
    spec: H1PointInTimeSpec,
) -> H1CapabilityProbe:
    """Run the read-only, parameterized supplier capability probe."""

    result = provider.probe(source_cutoff=spec.source_cutoff, max_history_sessions=spec.max_history_sessions)
    if not result.point_in_time_anchors_proven or result.adjustment_semantics != "qfq":
        return result
    return result


class H1PointInTimeDownloadService:
    def __init__(
        self,
        universe: H1UniverseProvider,
        history: H1PointInTimeProvider,
        archive: H1ArchivePort,
        *,
        workers: int = 5,
    ) -> None:
        if not 1 <= workers <= 5:
            raise ValueError("H1 workers must be in [1, 5]")
        self._universe = universe
        self._history = history
        self._archive = archive
        self._workers = workers

    def execute(
        self,
        spec: H1PointInTimeSpec,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> H1DownloadResult:
        securities = self._archive.registered_universe(spec.strategy)
        if not securities:
            securities = tuple(
                sorted(
                    {
                        item.code: item
                        for item in self._universe.fetch()
                        if item.board != "unsupported" and not item.is_st and not item.is_suspended
                    }.values(),
                    key=lambda item: item.code,
                )
            )
            self._archive.register_universe(spec, securities)
        completed = self._archive.completed_codes(spec.strategy)
        pending = tuple(item for item in securities if item.code not in completed)
        downloaded, failed = self._download_all(spec, pending, progress)
        return H1DownloadResult(
            spec.strategy,
            len(securities),
            len(set(completed).intersection(item.code for item in securities)),
            len(pending),
            downloaded,
            failed,
        )

    def _download_all(
        self,
        spec: H1PointInTimeSpec,
        pending: tuple[HistoricalSecurity, ...],
        progress: Callable[[int, int, str], None] | None,
    ) -> tuple[int, int]:
        downloaded = failed = processed = 0
        pending_iter = iter(pending)
        pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="score-h1")
        futures: dict[Future[tuple[H1PointInTimeRecord, ...]], str] = {}
        try:
            while len(futures) < self._workers:
                try:
                    item = next(pending_iter)
                except StopIteration:
                    break
                futures[pool.submit(self._fetch, item.code, spec)] = item.code
            while futures:
                finished, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in finished:
                    code = futures.pop(future)
                    processed += 1
                    if self._store_download(spec, code, future):
                        downloaded += 1
                    else:
                        failed += 1
                    if progress is not None:
                        progress(processed, len(pending), code)
                while len(futures) < self._workers:
                    try:
                        item = next(pending_iter)
                    except StopIteration:
                        break
                    futures[pool.submit(self._fetch, item.code, spec)] = item.code
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return downloaded, failed

    def _fetch(self, code: str, spec: H1PointInTimeSpec) -> tuple[H1PointInTimeRecord, ...]:
        return tuple(self._history.fetch(code, spec))

    def _store_download(
        self,
        spec: H1PointInTimeSpec,
        code: str,
        future: Future[tuple[H1PointInTimeRecord, ...]],
    ) -> bool:
        try:
            records = future.result()
            _validate_records(records, spec, code)
            self._archive.save_records(spec, code, records)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._archive.record_failure(spec, code, _error_code(exc))
            return False
        return True


def _validate_records(records: Sequence[H1PointInTimeRecord], spec: H1PointInTimeSpec, code: str) -> None:
    if not records or len(records) > spec.max_history_sessions:
        raise ValueError("H1 response has invalid bounded coverage")
    if any(item.code != code or item.strategy != spec.strategy for item in records):
        raise ValueError("H1 response identity mismatch")
    dates = tuple(item.trade_date for item in records)
    if dates != tuple(sorted(set(dates))):
        raise ValueError("H1 response dates are not unique and ordered")
    if dates[-1] > spec.source_cutoff:
        raise ValueError("H1 response exceeds source cutoff")
    expected_anchor = spec.anchor_kind
    if any(_anchor_kind(item) != expected_anchor for item in records):
        raise ValueError("H1 response anchor mismatch")


def _anchor_kind(record: H1PointInTimeRecord) -> str:
    return "today_1120" if record.strategy == "today" else "d25_1450" if record.strategy == "d25" else "tomorrow_1450"


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "network"
    if isinstance(exc, RuntimeError):
        return "provider_error"
    return "invalid_point_in_time_history"


__all__ = [
    "H1ArchivePort",
    "H1CapabilityProbePort",
    "H1DownloadResult",
    "H1PointInTimeDownloadService",
    "H1PointInTimeProvider",
    "H1UniverseProvider",
    "run_capability_probe",
]
