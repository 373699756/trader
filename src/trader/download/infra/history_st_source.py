"""Bounded public name-history checks; unknown histories never qualify as never-ST."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import Future, as_completed
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
        get: Callable[..., requests.Response],
        executor: WorkerExecutor,
        cancel_requested: Callable[[], bool],
        *,
        report: Callable[[int, int, HistoryStEligibilitySummary | None], None],
        batch_size: int = 8,
    ) -> None:
        if batch_size < 1:
            raise ValueError("history ST worker batch size must be positive")
        self._path = path
        self._get = get
        self._executor = executor
        self._cancel_requested = cancel_requested
        self._report = report
        self._batch_size = batch_size

    def fetch(self, universe: tuple[BaoStockSecurity, ...], as_of: date) -> tuple[HistoryStEvidence, ...]:
        evidence = {item.code: item for item in read_st_evidence(self._path)}
        total = len(universe)
        completed = 0
        for start in range(0, total, self._batch_size):
            self._check_cancel()
            futures: dict[Future[HistoryStEvidence], str] = {}
            try:
                for security in universe[start : start + self._batch_size]:
                    old = evidence.get(security.code)
                    if old is not None and (
                        old.status == "ever_st" or old.status == "clear" and old.checked_on == as_of
                    ):
                        completed += 1
                    else:
                        future = submit_or_reject(self._executor, self._fetch_code, security.code, security.name, as_of)
                        futures[future] = security.code
                for future in as_completed(futures):
                    self._check_cancel()
                    code = futures[future]
                    try:
                        evidence[code] = future.result()
                    except (requests.RequestException, OSError, ValueError):
                        evidence[code] = HistoryStEvidence(code, as_of, "unknown")
                    completed += 1
                write_st_evidence(self._path, tuple(sorted(evidence.values(), key=lambda item: item.code)))
                summary = None
                if completed == total:
                    current = tuple(evidence[item.code] for item in universe)
                    summary = HistoryStEligibilitySummary(
                        sum(item.status == "clear" for item in current),
                        sum(item.status == "ever_st" for item in current),
                        sum(item.status == "unknown" for item in current),
                    )
                self._report(completed, total, summary)
            finally:
                for future in futures:
                    future.cancel()
        codes = {item.code for item in universe}
        return tuple(item for code, item in sorted(evidence.items()) if code in codes)

    def _fetch_code(self, code: str, current_name: str, as_of: date) -> HistoryStEvidence:
        self._check_cancel()
        response = self._get(
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
