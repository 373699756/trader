"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO

from trader.download.domain.company_evidence import EvidenceSyncProgress


class CompanyEvidenceProgress:
    def __init__(self, stream: TextIO | None = None, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._monotonic = monotonic
        self._started = monotonic()
        self._last = 0.0
        self._component = ""

    def __call__(self, progress: EvidenceSyncProgress) -> None:
        now = self._monotonic()
        if progress.component == self._component and now - self._last < 2 and not progress.failure:
            return
        self._last, self._component = now, progress.component
        labels = {
            "st_eligibility": "ST资格",
            "financial": "财务增量",
            "pledge": "最新质押",
            "unlock": "未来90天解禁",
            "announcements": "巨潮日期分片",
            "risk_documents": "风险正文核验",
            "news": "候选新闻",
            "complete": "本轮结束",
        }
        print(
            f"risk_download | {labels[progress.component]} | 已完成分片 {progress.completed} | "
            f"待处理 {progress.pending} | 成功页 {progress.fetched_pages} | 变更行 {progress.changed_rows} | "
            f"本地复用 {progress.reused_partitions} | 累计 {now - self._started:.1f}秒"
            + (f" | 待续传 {progress.failure}" if progress.failure else ""),
            file=self._stream,
            flush=True,
        )


__all__ = ["CompanyEvidenceProgress"]
