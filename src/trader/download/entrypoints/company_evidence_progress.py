"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TextIO

from trader.download.application.progress_message import ProgressDuration
from trader.download.domain.company_evidence import DisclosurePartition, EvidenceSyncProgress


class CompanyEvidenceProgress:
    def __init__(self, stream: TextIO | None = None, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._monotonic = monotonic
        self._started = monotonic()
        self._last = 0.0
        self._previous: EvidenceSyncProgress | None = None

    def __call__(self, progress: EvidenceSyncProgress) -> None:
        now = self._monotonic()
        previous = self._previous
        boundary = (
            previous is None
            or progress.component != previous.component
            or progress.status != previous.status
            or progress.failure != previous.failure
            or (progress.pending == 0 and previous.pending != 0)
        )
        if not boundary and now - self._last < 2:
            return
        self._last, self._previous = now, progress
        labels = {
            "st_eligibility": "ST资格",
            "financial": "财务增量",
            "pledge": "最新质押",
            "unlock": "未来90天解禁",
            "announcements": "巨潮日期分片",
            "risk_documents": "风险正文核验",
            "news": "候选新闻",
            "publish": "发布公司投影",
            "complete": "本轮结束",
        }
        units = {
            "st_eligibility": "股",
            "financial": "页",
            "pledge": "页",
            "unlock": "页",
            "announcements": "分片",
            "risk_documents": "篇",
            "news": "股",
            "publish": "股",
            "complete": "股",
        }
        statuses = {"running": "进行中", "complete": "完成", "reused": "本地复用", "deferred": "待续传"}
        status = "complete" if progress.component == "st_eligibility" and progress.pending == 0 else progress.status
        print(
            f"download_risk | {labels[progress.component]} | {_counts(progress, units[progress.component])}"
            + _scope(progress)
            + f" | 成功页 {progress.fetched_pages} | 变更行 {progress.changed_rows} | "
            f"累计复用 {progress.reused_partitions} | 累计 {ProgressDuration(now - self._started)} | "
            f"状态 {statuses[status]}" + (f" | 原因 {progress.failure}" if progress.failure else ""),
            file=self._stream,
            flush=True,
        )


def _counts(progress: EvidenceSyncProgress, unit: str) -> str:
    if progress.component == "complete":
        return (
            f"已发布投影 {progress.completed} 股 | 待补公告 {progress.pending} 分片 | "
            f"待补正文 {progress.pending_documents} 篇"
        )
    if progress.status == "reused":
        return "无需请求，复用有效缓存"
    if progress.pending is None:
        return f"进度 {progress.completed}/? {unit}（总数待确认）"
    total = progress.completed + progress.pending
    percent = f"{100 * progress.completed / total:.2f}%" if total else "无待处理"
    return f"进度 {progress.completed}/{total} {unit}（{percent}） | 待处理 {progress.pending} {unit}"


def _scope(progress: EvidenceSyncProgress) -> str:
    scope = progress.scope
    if scope is None:
        return ""
    market = (
        {"sse": "沪市", "szse": "深市", "bse": "北市"}[scope.market] if isinstance(scope, DisclosurePartition) else ""
    )
    return f" | 当前 {market}{scope.start.isoformat()}~{scope.end.isoformat()} 第{scope.page}页"


__all__ = ["CompanyEvidenceProgress"]
