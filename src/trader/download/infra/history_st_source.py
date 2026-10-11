"""Bounded public name-history checks; unknown histories never qualify as never-ST."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path

import requests

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistoryStEligibilitySummary
from trader.download.domain.security_eligibility import is_st_security_name
from trader.download.infra.history_reference_files import read_st_evidence, write_st_evidence
from trader.infra.workers import WorkerExecutor, submit_or_reject

_NAME_HISTORY = re.compile(r"证券简称更名历史[：:]\s*</td>\s*<td[^>]*>(.*?)</td>", re.S)
_CHECKPOINT_CODES = 64


def parse_name_history_st(code: str, current_name: str, checked_on: date, text: str) -> HistoryStEvidence:
    match = _NAME_HISTORY.search(text)
    if match is None:
        raise ValueError("history_st_name_history_missing")
    names = unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", " ", match[1]))).strip()
    current = unicodedata.normalize("NFKC", current_name).strip()
    if names in {"--", "-", "暂无", "暂无数据"} or not current:
        raise ValueError("history_st_name_history_empty")
    return HistoryStEvidence(
        code,
        checked_on,
        "ever_st" if is_st_security_name(f"{current} {names}") else "clear",
    )


class HistoryStNameSource:
    def __init__(  # noqa: PLR0913 - explicit supplier and cancellation boundaries
        self,
        path: Path,
        session_factory: Callable[[], AbstractContextManager[requests.Session]],
        executor: WorkerExecutor,
        cancel_requested: Callable[[], bool],
        *,
        report: Callable[[int, int, HistoryStEligibilitySummary | None], None],
        batch_size: int = 8,
    ) -> None:
        if batch_size < 1:
            raise ValueError("history ST worker batch size must be positive")
        self._path = path
        self._session_factory = session_factory
        self._executor = executor
        self._cancel_requested = cancel_requested
        self._report = report
        self._batch_size = batch_size

    def fetch(self, universe: tuple[BaoStockSecurity, ...], as_of: date) -> tuple[HistoryStEvidence, ...]:
        evidence = {item.code: item for item in read_st_evidence(self._path)}
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
        reported = completed
        summary_reported = False
        dirty = 0
        next_security = 0
        futures: dict[Future[HistoryStEvidence], str] = {}
        try:
            while futures or next_security < len(pending):
                self._check_cancel()
                next_security = self._fill_checks(pending, next_security, futures, as_of)
                done, _remaining = wait(tuple(futures), return_when=FIRST_COMPLETED)
                self._check_cancel()
                accepted = self._accept_checks(done, futures, evidence, as_of)
                completed += accepted
                dirty += accepted
                if dirty >= _CHECKPOINT_CODES:
                    self._write(evidence)
                    dirty = 0
                if completed == total and dirty:
                    self._write(evidence)
                    dirty = 0
                if completed == total or completed - reported >= self._batch_size:
                    summary = self._summary(universe, evidence) if completed == total else None
                    self._report(completed, total, summary)
                    summary_reported = summary is not None
                    reported = completed
        finally:
            for future in futures:
                future.cancel()
        if dirty:
            self._write(evidence)
        if completed == total and not summary_reported:
            self._report(completed, total, self._summary(universe, evidence))
        codes = {item.code for item in universe}
        return tuple(item for code, item in sorted(evidence.items()) if code in codes)

    def _fill_checks(
        self,
        pending: tuple[BaoStockSecurity, ...],
        next_security: int,
        futures: dict[Future[HistoryStEvidence], str],
        as_of: date,
    ) -> int:
        while len(futures) < self._batch_size and next_security < len(pending):
            security = pending[next_security]
            next_security += 1
            future = submit_or_reject(self._executor, self._fetch_code, security.code, security.name, as_of)
            futures[future] = security.code
        return next_security

    @staticmethod
    def _accept_checks(
        done: set[Future[HistoryStEvidence]],
        futures: dict[Future[HistoryStEvidence], str],
        evidence: dict[str, HistoryStEvidence],
        as_of: date,
    ) -> int:
        for future in done:
            code = futures.pop(future)
            try:
                evidence[code] = future.result()
            except (requests.RequestException, OSError, ValueError):
                evidence[code] = HistoryStEvidence(code, as_of, "unknown")
        return len(done)

    def _write(self, evidence: dict[str, HistoryStEvidence]) -> None:
        write_st_evidence(self._path, tuple(sorted(evidence.values(), key=lambda item: item.code)))

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

    def _fetch_code(self, code: str, current_name: str, as_of: date) -> HistoryStEvidence:
        self._check_cancel()
        with self._session_factory() as session:
            response = session.get(
                f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCI_CorpInfo/stockid/{code}.phtml",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15.0,
            )
            try:
                response.raise_for_status()
                text = response.content.decode("gb18030", errors="strict")
                return parse_name_history_st(code, current_name, as_of, text)
            finally:
                response.close()

    def _check_cancel(self) -> None:
        if self._cancel_requested():
            raise RuntimeError("cancelled")
