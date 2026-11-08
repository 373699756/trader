"""Bounded public name-history checks; unknown histories never qualify as never-ST."""

from __future__ import annotations

import html
import re
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistoryStCheckFailure, HistoryStCheckProgress, HistoryStEligibilitySummary
from trader.download.domain.security_eligibility import IsMissingNameHistory, IsStSecurityName
from trader.download.infra.history_sync_failure import HistoryFailureCode, HistoryFailureDetail
from trader.download.infra.reference_index import SQLiteReferenceIndex
from trader.infra.http_contracts import HttpCancelled, HttpConnectionError, HttpError, HttpStatusError, HttpTimeout, HttpTransport
from trader.infra.workers import SubmitWithBackpressure, WorkerExecutor

_NAME_HISTORY = re.compile(r"证券简称更名历史[：:]\s*</td>\s*<td[^>]*>(.*?)</td>", re.S)
_CHECKPOINT_CODES = 64
_ATTEMPT_SECONDS = 15.0
_TOTAL_SECONDS = 30.0
_MAXIMUM_ATTEMPTS = 2
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class _StCheckResult:
    evidence: HistoryStEvidence
    failure: HistoryStCheckFailure | None = None


def ParseNameHistorySt(code: str, current_name: str, checked_on: date, text: str) -> HistoryStEvidence:
    match = _NAME_HISTORY.search(text)
    if match is None:
        raise ValueError("history_st_name_history_missing")
    names = unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", " ", match[1]))).strip()
    current = unicodedata.normalize("NFKC", current_name).strip()
    if IsMissingNameHistory(names) or not current:
        raise ValueError("history_st_name_history_empty")
    return HistoryStEvidence(
        code,
        checked_on,
        "ever_st" if IsStSecurityName(f"{current} {names}") else "clear",
    )


class HistoryStNameSource:
    def __init__(  # noqa: PLR0913 - explicit supplier and cancellation boundaries
        self,
        index: SQLiteReferenceIndex,
        session_factory: Callable[[], AbstractContextManager[HttpTransport]],
        executor: WorkerExecutor,
        cancel_requested: Callable[[], bool],
        *,
        report: Callable[[HistoryStCheckProgress], None],
        batch_size: int = 8,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if batch_size < 1:
            raise ValueError("history ST worker batch size must be positive")
        self._index = index
        self._session_factory = session_factory
        self._executor = executor
        self._cancel_requested = cancel_requested
        self._report = report
        self._batch_size = batch_size
        self._monotonic, self._sleep = monotonic, sleeper
        self._observed: tuple[HistoryStEvidence, ...] = ()

    def fetch(self, universe: tuple[BaoStockSecurity, ...], as_of: date) -> tuple[HistoryStEvidence, ...]:
        self._check_cancel()
        self._observed = self._index.read_st()
        evidence = {item.code: item for item in self._observed}
        for security in universe:
            previous = evidence.get(security.code)
            if IsStSecurityName(security.name) and (previous is None or previous.status != "ever_st"):
                evidence[security.code] = HistoryStEvidence(security.code, as_of, "ever_st")
        if any(evidence.get(item.code) != item for item in self._observed) or len(evidence) != len(self._observed):
            self._write(evidence)
        total = len(universe)
        pending = tuple(
            security
            for security in universe
            if not (
                (old := evidence.get(security.code)) is not None
                and (old.status == "ever_st" or old.status == "clear" and old.checked_on == as_of)
            )
        )
        completed = total - len(pending)
        persisted = completed
        if pending:
            self._report(HistoryStCheckProgress(completed, persisted, total))
        reported = completed
        dirty = 0
        next_security = 0
        futures: dict[Future[_StCheckResult], str] = {}
        try:
            while futures or next_security < len(pending):
                self._check_cancel()
                next_security = self._fill_checks(pending, next_security, futures, as_of)
                done, _remaining = wait(tuple(futures), timeout=0.1, return_when=FIRST_COMPLETED)
                self._check_cancel()
                accepted, failures = self._accept_checks(done, futures, evidence)
                completed += accepted
                dirty += accepted
                if dirty >= _CHECKPOINT_CODES or completed == total and dirty:
                    self._report(HistoryStCheckProgress(completed, persisted, total, failures=failures))
                    self._write(evidence)
                    persisted = completed
                    dirty = 0
                    self._report(HistoryStCheckProgress(completed, persisted, total))
                    reported = completed
                elif failures or completed - reported >= self._batch_size:
                    self._report(HistoryStCheckProgress(completed, persisted, total, failures=failures))
                    reported = completed
        finally:
            for future in futures:
                future.cancel()
        self._write(evidence)
        self._report(HistoryStCheckProgress(completed, completed, total, self._summary(universe, evidence), cache_only=not pending))
        codes = {item.code for item in universe}
        return tuple(item for code, item in sorted(evidence.items()) if code in codes)

    def _fill_checks(
        self,
        pending: tuple[BaoStockSecurity, ...],
        next_security: int,
        futures: dict[Future[_StCheckResult], str],
        as_of: date,
    ) -> int:
        while len(futures) < self._batch_size and next_security < len(pending):
            security = pending[next_security]
            next_security += 1
            future = SubmitWithBackpressure(self._executor, self._fetch_code, security.code, security.name, as_of)
            futures[future] = security.code
        return next_security

    def _accept_checks(
        self,
        done: set[Future[_StCheckResult]],
        futures: dict[Future[_StCheckResult], str],
        evidence: dict[str, HistoryStEvidence],
    ) -> tuple[int, tuple[HistoryStCheckFailure, ...]]:
        failures = []
        for future in done:
            self._check_cancel()
            code = futures.pop(future)
            result = future.result()
            self._check_cancel()
            evidence[code] = result.evidence
            if result.failure is not None:
                failures.append(result.failure)
        return len(done), tuple(failures)

    def _write(self, evidence: dict[str, HistoryStEvidence]) -> None:
        self._check_cancel()
        merged = self._index.merge_st(tuple(sorted(evidence.values(), key=lambda item: item.code)), self._observed)
        self._check_cancel()
        self._observed = merged
        evidence.update((item.code, item) for item in merged)

    @staticmethod
    def _summary(
        universe: tuple[BaoStockSecurity, ...], evidence: dict[str, HistoryStEvidence]
    ) -> HistoryStEligibilitySummary:
        current = tuple(evidence[item.code] for item in universe)
        return HistoryStEligibilitySummary(
            sum(item.status == "clear" for item in current),
            sum(item.status == "ever_st" for item in current),
            sum(item.status == "unknown" for item in current),
        )

    def _fetch_code(self, code: str, current_name: str, as_of: date) -> _StCheckResult:
        self._check_cancel()
        started = self._monotonic()
        deadline = started + _TOTAL_SECONDS
        attempts = 0
        timeout = _ATTEMPT_SECONDS
        while True:
            self._check_cancel()
            attempted = False
            try:
                with self._session_factory() as session:
                    self._check_cancel()
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        raise HttpTimeout("history_st_total_budget_exhausted")
                    timeout = min(_ATTEMPT_SECONDS, remaining)
                    attempts += 1
                    attempted = True
                    response = session.get(
                        f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{code}.phtml",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, deadline=deadline,
                        cancel_requested=self._cancel_requested,
                    )
                    self._check_cancel()
                    response.raise_for_status()
                    text = response.content.decode("gb18030", errors="strict")
                    evidence = ParseNameHistorySt(code, current_name, as_of, text)
                    self._check_cancel()
                    if self._monotonic() > deadline:
                        raise HttpTimeout("history_st_total_budget_exhausted")
                    return _StCheckResult(evidence)
            except HttpCancelled:
                raise
            except (HttpError, OSError, ValueError) as exc:
                self._check_cancel()
                retryable = isinstance(exc, (HttpTimeout, HttpConnectionError)) or (
                    isinstance(exc, HttpStatusError) and exc.status_code in _RETRYABLE_STATUSES
                )
                if retryable and attempted and attempts < _MAXIMUM_ATTEMPTS and self._monotonic() < deadline:
                    self._retry_wait(0.25, deadline)
                    if self._monotonic() < deadline:
                        continue
                failure = HistoryStCheckFailure(
                    code, current_name, _failure_reason(exc), HistoryFailureDetail(exc), attempts, _MAXIMUM_ATTEMPTS,
                    max(0.0, self._monotonic() - started), timeout,
                )
                return _StCheckResult(HistoryStEvidence(code, as_of, "unknown"), failure)

    def _retry_wait(self, seconds: float, deadline: float) -> None:
        while seconds > 0:
            self._check_cancel()
            duration = min(seconds, 0.05, max(0.0, deadline - self._monotonic()))
            if duration <= 0:
                return
            self._sleep(duration)
            seconds -= duration
        self._check_cancel()

    def _check_cancel(self) -> None:
        if self._cancel_requested():
            raise RuntimeError("cancelled")


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, HttpStatusError):
        return f"http_{exc.status_code}"
    if isinstance(exc, HttpTimeout):
        return "history_st_timeout"
    if isinstance(exc, HttpConnectionError):
        return "history_st_connection_failed"
    if isinstance(exc, UnicodeDecodeError):
        return "history_st_decode_failed"
    return HistoryFailureCode(exc)
