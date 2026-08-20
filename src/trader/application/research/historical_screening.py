"""Explicit, bounded download orchestration for retrospective score screening."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.historical_screening import HistoricalPriceBar, HistoricalScreeningSpec

ResearchBoard = Literal["main", "chinext", "star", "unsupported"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class HistoricalSecurity:
    code: str
    board: ResearchBoard
    name: str
    is_st: bool
    is_suspended: bool

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("historical security code is invalid")
        if self.board not in {"main", "chinext", "star", "unsupported"}:
            raise ValueError("historical security board is invalid")


@dataclass(frozen=True)
class HistoricalDownloadResult:
    research_identity: str
    universe_count: int
    previously_completed: int
    attempted: int
    downloaded: int
    failed: int


@dataclass(frozen=True)
class HistoricalArchiveStatus:
    initialized: bool = False
    research_identity: str = ""
    universe_count: int = 0
    completed_codes: int = 0
    failed_codes: int = 0
    bar_count: int = 0
    first_trade_date: str | None = None
    last_trade_date: str | None = None
    spec_hash: str = ""


@dataclass(frozen=True, order=True)
class HistoricalHistoryIdentity:
    code: str
    bar_count: int
    content_hash: str

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("historical history identity code is invalid")
        if self.bar_count < 1 or _SHA256.fullmatch(self.content_hash) is None:
            raise ValueError("historical history identity content is invalid")


@dataclass(frozen=True)
class HistoricalArchiveManifest:
    research_identity: str
    spec_hash: str
    universe_hash: str
    histories_hash: str
    histories: tuple[HistoricalHistoryIdentity, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        histories = tuple(sorted(self.histories, key=lambda item: item.code))
        if len({item.code for item in histories}) != len(histories):
            raise ValueError("historical archive manifest contains duplicate codes")
        for value in (self.universe_hash, self.histories_hash):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("historical archive manifest hash is invalid")
        if self.spec_hash and _SHA256.fullmatch(self.spec_hash) is None:
            raise ValueError("historical archive spec hash is invalid")
        object.__setattr__(self, "histories", histories)
        object.__setattr__(self, "content_hash", canonical_hash(self))


class HistoricalUniverseProvider(Protocol):
    def fetch(self) -> Sequence[HistoricalSecurity]: ...


class HistoricalPriceProvider(Protocol):
    def fetch_history(self, code: str, *, days: int) -> Sequence[HistoricalPriceBar]: ...


class HistoricalArchivePort(Protocol):
    def registered_universe(self, research_identity: str) -> tuple[HistoricalSecurity, ...]: ...

    def register_universe(
        self,
        spec: HistoricalScreeningSpec,
        universe: Sequence[HistoricalSecurity],
    ) -> None: ...

    def completed_codes(self, research_identity: str) -> frozenset[str]: ...

    def save_history(
        self,
        spec: HistoricalScreeningSpec,
        code: str,
        bars: Sequence[HistoricalPriceBar],
    ) -> None: ...

    def record_failure(self, spec: HistoricalScreeningSpec, code: str, error_code: str) -> None: ...


ProgressCallback = Callable[[int, int, str], None]


class HistoricalDownloadService:
    def __init__(
        self,
        universe: HistoricalUniverseProvider,
        history: HistoricalPriceProvider,
        archive: HistoricalArchivePort,
        *,
        workers: int,
    ) -> None:
        if not 1 <= workers <= 5:
            raise ValueError("historical screening workers must be in [1, 5]")
        self._universe = universe
        self._history = history
        self._archive = archive
        self._workers = workers

    def execute(
        self,
        spec: HistoricalScreeningSpec,
        *,
        progress: ProgressCallback | None = None,
    ) -> HistoricalDownloadResult:
        securities = self._archive.registered_universe(spec.research_identity)
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
        completed = self._archive.completed_codes(spec.research_identity)
        pending = tuple(item for item in securities if item.code not in completed)
        downloaded, failed = self._download_all(spec, pending, progress)
        return HistoricalDownloadResult(
            research_identity=spec.research_identity,
            universe_count=len(securities),
            previously_completed=len(set(completed).intersection(item.code for item in securities)),
            attempted=len(pending),
            downloaded=downloaded,
            failed=failed,
        )

    def _download_all(
        self,
        spec: HistoricalScreeningSpec,
        pending: tuple[HistoricalSecurity, ...],
        progress: ProgressCallback | None,
    ) -> tuple[int, int]:
        downloaded = failed = processed = 0
        pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="score-history")
        pending_iter = iter(pending)
        futures: dict[Future[tuple[HistoricalPriceBar, ...]], str] = {}
        try:
            for item in pending_iter:
                futures[pool.submit(self._fetch, item.code, spec.download_sessions)] = item.code
                if len(futures) == self._workers:
                    break
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
                    futures[pool.submit(self._fetch, item.code, spec.download_sessions)] = item.code
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return downloaded, failed

    def _store_download(
        self,
        spec: HistoricalScreeningSpec,
        code: str,
        future: Future[tuple[HistoricalPriceBar, ...]],
    ) -> bool:
        try:
            bars = future.result()
            _validate_history(
                bars,
                source_cutoff=spec.source_cutoff,
                minimum=spec.minimum_history_sessions + spec.label_horizon_sessions,
                maximum=spec.download_sessions,
            )
            self._archive.save_history(spec, code, bars)
        except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._archive.record_failure(spec, code, _error_code(exc))
            return False
        return True

    def _fetch(self, code: str, days: int) -> tuple[HistoricalPriceBar, ...]:
        return tuple(self._history.fetch_history(code, days=days))


def _validate_history(
    bars: Sequence[HistoricalPriceBar],
    *,
    source_cutoff: date,
    minimum: int,
    maximum: int,
) -> None:
    if not minimum <= len(bars) <= maximum:
        raise ValueError("historical screening response has invalid coverage")
    dates = tuple(item.trade_date for item in bars)
    if dates != tuple(sorted(set(dates))):
        raise ValueError("historical screening dates are not unique and ordered")
    if dates[-1] > source_cutoff:
        raise ValueError("historical screening response exceeds the source cutoff")
    if any(item.adjustment != "qfq" for item in bars):
        raise ValueError("historical screening requires qfq bars")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "network"
    if isinstance(exc, RuntimeError):
        return "provider_error"
    return "invalid_history"


__all__ = [
    "HistoricalDownloadResult",
    "HistoricalDownloadService",
    "HistoricalArchiveManifest",
    "HistoricalArchiveStatus",
    "HistoricalHistoryIdentity",
    "HistoricalSecurity",
]
