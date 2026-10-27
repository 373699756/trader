"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TextIO

from trader.download.application.progress_message import ProgressDuration
from trader.download.domain.company_evidence import CompanyStockProgress, EvidenceSyncProgress
from trader.download.domain.history_sync import HistoryStEligibilitySummary


def ReportStPreparation(
    progress: Callable[[EvidenceSyncProgress], None] | None,
    completed: int,
    total: int,
    summary: HistoryStEligibilitySummary | None,
) -> None:
    if progress is not None:
        progress(
            EvidenceSyncProgress(
                "st_eligibility",
                completed,
                total - completed,
                0,
                0,
                0,
                f"unknown:{summary.unknown}" if summary and summary.unknown else "",
            )
        )


def ReportCompanyStock(
    report: Callable[[EvidenceSyncProgress], None],
    names: tuple[tuple[str, str], ...],
    stock: CompanyStockProgress,
) -> None:
    name = next((name for code, name in names if code == stock.code), "")
    report(
        EvidenceSyncProgress(
            "publish", stock.processed, stock.total - stock.processed, 0, 0, 0, stock=replace(stock, name=name)
        )
    )


class CompanyEvidenceProgress:
    def __init__(self, stream: TextIO | None = None, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._monotonic = monotonic
        self._started = monotonic()
        self._component_started = self._started
        self._last = 0.0
        self._previous: EvidenceSyncProgress | None = None
        self._stocks: dict[str, CompanyStockProgress] = {}

    def __call__(self, progress: EvidenceSyncProgress) -> None:
        now = self._monotonic()
        previous = self._previous
        if progress.component == "publish" and progress.stock is not None:
            self._stocks[progress.stock.code] = progress.stock
        if previous is None or progress.component != previous.component:
            self._component_started = now
        boundary = (
            previous is None
            or progress.component != previous.component
            or progress.status != previous.status
            or progress.activity != previous.activity
            or progress.failure != previous.failure
            or (progress.pending == 0 and previous.pending != 0)
        )
        if not boundary and now - self._last < 1:
            return
        self._last, self._previous = now, progress
        labels = {
            "st_eligibility": "ST资格",
            "calendar": "交易日历",
            "financial": "财务增量",
            "pledge": "最新质押",
            "unlock": "未来180天解禁",
            "announcements": "公告增量下载",
            "risk_documents": "公告正文分析",
            "news": "候选新闻",
            "publish": "发布公司投影",
            "complete": "本轮结束",
        }
        units = {
            "st_eligibility": "股",
            "calendar": "份",
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
        failure = progress.failure
        if progress.status == "running" and previous is not None and progress.component == previous.component:
            reported = set(previous.failure.split(","))
            failure = ",".join(item for item in failure.split(",") if item and item not in reported)
        stock = _stock(progress)
        announcement_stock = stock if progress.component == "announcements" else ""
        other_stock = "" if progress.component == "announcements" else stock
        print(
            f"sync company | {labels[progress.component]}{announcement_stock} | {_counts(progress, units[progress.component])}"
            + other_stock
            + (self._summary(progress.completed) if progress.component == "complete" else "")
            + (" | 补查历史发行人（保留已完成页）" if progress.activity == "repair" else "")
            + f" | 成功页 {progress.fetched_pages} | 变更行 {progress.changed_rows} | "
            f"累计复用 {progress.reused_partitions} | 本阶段 {ProgressDuration(now - self._component_started)} | "
            f"命令累计 {ProgressDuration(now - self._started)} | "
            f"状态 {statuses[status]}" + (f" | 原因 {failure}" if failure else ""),
            file=self._stream,
            flush=True,
        )

    def _summary(self, published: int) -> str:
        if not self._stocks:
            return ""
        stocks = tuple(self._stocks.values())
        complete = sum(stock.coverage_complete for stock in stocks)
        reused = sum(stock.reused for stock in stocks)
        return (
            f" | 已处理 {len(stocks)} 股 | 完整 {complete} 股 | 部分 {len(stocks) - complete} 股"
            f" | 未发布 {max(0, len(stocks) - published)} 股 | 更新 {len(stocks) - reused} 股 | 复用 {reused} 股"
            f" | 风险 {sum(stock.risks for stock in stocks)} 条 | 利好 {sum(stock.positives for stock in stocks)} 条"
            f" | 待核实 {sum(stock.review_count for stock in stocks)} 项"
        )


def _counts(progress: EvidenceSyncProgress, unit: str) -> str:
    if progress.component == "complete":
        return (
            f"已发布 {progress.completed} 股（含待核实） | 公告覆盖{'待补' if progress.pending else '已核对'} | "
            f"待补正文 {progress.pending_documents} 篇"
        )
    if progress.status == "reused":
        return "无需请求，复用有效缓存"
    if progress.pending is None:
        return f"进度 {progress.completed}/? {unit}（总数待确认）"
    if progress.component == "announcements":
        return (
            f"本轮涉及公告 {progress.stock.processed}/{progress.stock.total} 股（覆盖核验中）"
            if progress.stock is not None
            else "正在定位待补公告，股票总进度待核验"
        )
    if progress.component == "publish" and progress.stock is not None:
        return f"已分析股票 {progress.stock.processed}/{progress.stock.total} 股（等待原子发布）"
    return _page_counts(progress, unit, progress.pending)


def _page_counts(progress: EvidenceSyncProgress, unit: str, pending: int) -> str:
    total = progress.completed + pending
    if progress.component == "risk_documents":
        return (
            f"已尝试 {progress.attempted_documents}/{total} 篇 | 已核验 {progress.completed} 篇 | "
            f"暂缓重试 {progress.deferred_documents} 篇 | 待补 {progress.pending} 篇"
        )
    percent = f"{100 * progress.completed / total:.2f}%" if total else "无待处理"
    return f"进度 {progress.completed}/{total} {unit}（{percent}） | 待处理 {progress.pending} {unit}"


def _stock(progress: EvidenceSyncProgress) -> str:
    stock = progress.stock
    if stock is None:
        return ""
    if progress.component == "announcements":
        return (
            f" | 最近处理公告目录 {stock.code} {stock.name} | 本轮该股页记录 {stock.announcements} 篇 | 正文分析未开始"
        )
    return (
        f" | 当前 {stock.code} {stock.name} | 公告 {stock.announcements} 篇"
        f" | 已分析 {stock.analyzed} 篇 | 待分析 {stock.pending} 篇"
        + (
            f" | 风险 {stock.risks} 条 | 利好 {stock.positives} 条 | 待核实 {stock.review_count} 项"
            + (" | 资料完整" if stock.coverage_complete else " | 资料部分覆盖")
            if progress.component == "publish"
            else " | 事件待核验"
        )
        + (" | 已发布" if stock.published else " | 处理中")
    )


__all__ = ["CompanyEvidenceProgress", "ReportStPreparation"]
