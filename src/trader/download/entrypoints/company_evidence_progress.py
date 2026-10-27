"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from math import ceil
from typing import TextIO

from trader.download.application.progress_message import ProgressDuration
from trader.download.domain.company_evidence import CompanyStockProgress, DisclosurePartition, EvidenceSyncProgress
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
            "facts", stock.processed, stock.total - stock.processed, 0, 0, 0, stock=replace(stock, name=name)
        )
    )
    if stock.total and stock.processed == stock.total:
        report(EvidenceSyncProgress("publish", 0, stock.total, 0, 0, 0))


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
        if progress.component == "facts" and progress.stock is not None:
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
            "financial": "财务增量下载",
            "pledge": "质押快照下载",
            "unlock": "解禁计划下载",
            "announcements": "公告目录下载",
            "risk_documents": "公告正文下载提取",
            "news": "候选新闻",
            "facts": "逐股事件核验",
            "publish": "公司投影发布",
            "complete": "本轮结束",
        }
        units = {
            "st_eligibility": "股",
            "calendar": "份",
            "financial": "页",
            "pledge": "页",
            "unlock": "页",
            "announcements": "范围",
            "risk_documents": "篇",
            "news": "股",
            "facts": "股",
            "publish": "股",
            "complete": "股",
        }
        statuses = {"running": "进行中", "complete": "完成", "reused": "本地复用", "deferred": "待续传"}
        status = "complete" if progress.component == "st_eligibility" and progress.pending == 0 else progress.status
        status_text = statuses[status]
        if progress.component == "news" and progress.completed == progress.pending == 0:
            status_text = "未覆盖"
        failure = progress.failure
        if progress.status == "running" and previous is not None and progress.component == previous.component:
            reported = set(previous.failure.split(","))
            failure = ",".join(item for item in failure.split(",") if item and item not in reported)
        print(
            f"sync company | {labels[progress.component]} | {_counts(progress, units[progress.component])}"
            + _scope(progress)
            + _stock(progress)
            + (self._summary(progress.completed) if progress.component == "complete" else "")
            + (" | 补查历史发行人（保留已完成页）" if progress.activity == "repair" else "")
            + (
                f" | 本轮成功页 {progress.fetched_pages} | 原始资料变更 {progress.changed_rows} 条（非每股发布行）"
                f" | 本轮缓存复用 {progress.reused_partitions} 次"
                if progress.component == "complete"
                else ""
            )
            + f" | 本阶段 {ProgressDuration(now - self._component_started)} | "
            f"命令累计 {ProgressDuration(now - self._started)} | "
            f"状态 {status_text}" + (f" | 原因 {_failure_text(failure)}" if failure else "") + _recovery(progress),
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
            f"已发布 {progress.completed} 股（含待核实） | 公告待补 {progress.pending} 个范围 | "
            f"待补正文 {progress.pending_documents} 篇"
        )
    if progress.status == "reused":
        return "无需请求，复用有效缓存"
    if progress.component == "announcements":
        dates = progress.dates
        if dates is None:
            return "正在规划日期范围" if progress.pending is None else f"待补 {progress.pending} 个日期范围"
        return (
            f"已核对日期 {dates.completed_days}/{dates.total_days} 天（全部目标市场）"
            f" | 窗口 {dates.window_start} 至 {dates.window_end} | 待补 {progress.pending} 个范围"
        )
    if progress.component == "news" and progress.completed == progress.pending == 0:
        return "无候选名单，本轮未抓取新闻"
    if progress.pending is None:
        return f"进度 {progress.completed}/? {unit}（总数待确认）"
    if progress.component == "facts" and progress.stock is not None:
        return f"已核验股票 {progress.stock.processed}/{progress.stock.total} 股（等待原子发布）"
    if progress.component == "publish":
        total = progress.completed + progress.pending
        return (
            f"已发布 {progress.completed}/{total} 股（含待核实）"
            if progress.status == "complete"
            else f"已核验 {total} 股，正在原子提交（尚未发布）"
        )
    return _page_counts(progress, unit, progress.pending)


def _page_counts(progress: EvidenceSyncProgress, unit: str, pending: int) -> str:
    total = progress.completed + pending
    if progress.component == "risk_documents":
        return (
            f"已尝试 {progress.attempted_documents}/{total} 篇 | 已提取正文 {progress.completed} 篇 | "
            f"本轮失败 {progress.failed_documents} 篇 | 冷却暂缓 {progress.deferred_documents} 篇 | 待补 {progress.pending} 篇"
        )
    percent = f"{100 * progress.completed / total:.2f}%" if total else "无待处理"
    return f"进度 {progress.completed}/{total} {unit}（{percent}） | 待处理 {progress.pending} {unit}"


def _stock(progress: EvidenceSyncProgress) -> str:
    stock = progress.stock
    if stock is None or progress.component == "announcements":
        return ""
    if progress.component == "risk_documents":
        return f" | 当前正文股票 {stock.code} {stock.name}"
    if progress.component == "news":
        return f" | 当前新闻股票 {stock.code} {stock.name}"
    return (
        f" | 当前 {stock.code} {stock.name} | 公告 {stock.announcements} 篇"
        f" | 已提取正文 {stock.analyzed} 篇 | 待提取正文 {stock.pending} 篇"
        + (
            f" | 风险 {stock.risks} 条 | 利好 {stock.positives} 条 | 待核实 {stock.review_count} 项"
            + (" | 资料完整" if stock.coverage_complete else " | 资料部分覆盖")
            if progress.component == "facts"
            else " | 事件待核验"
        )
        + (" | 已发布" if stock.published else " | 处理中")
    )


def _scope(progress: EvidenceSyncProgress) -> str:
    scope = progress.scope
    if scope is None:
        return ""
    if progress.component == "announcements" and isinstance(scope, DisclosurePartition):
        market = {"sse": "上交所", "szse": "深交所", "bse": "北交所"}[scope.market]
        dates = str(scope.start) if scope.start == scope.end else f"{scope.start} 至 {scope.end}"
        pages = (
            f"/{ceil(scope.expected_rows / 30) or 1}"
            if scope.expected_rows is not None and scope.expected_rows <= 3000
            else ""
        )
        return f" | 当前日期 {dates} | {market} | 本查询第 {scope.page}{pages} 页"
    label = {"financial": "披露日期", "pledge": "快照日期", "unlock": "前视日期"}.get(progress.component)
    return f" | {label} {scope.start} 至 {scope.end}" if label else ""


def _failure_text(failure: str) -> str:
    scopes = {
        "announcements": "公告目录",
        "risk_documents": "公告正文",
        "task": "整轮任务",
        "financial": "财务",
        "pledge": "质押",
        "unlock": "解禁",
        "news": "新闻",
    }
    reasons = {
        "source_stopped": "来源暂缓（连续失败或限流）",
        "disclosure_document_no_text": "PDF未提取到文字",
        "disclosure_document_short_text": "PDF文字不足80字",
        "disclosure_document_parse_timeout": "PDF解析超时",
        "disclosure_document_unreadable": "PDF无法解析",
        "disclosure_document_oversized": "PDF超过大小上限",
        "disclosure_document_encrypted": "PDF已加密",
        "access_denied": "来源拒绝访问",
        "rate_limited": "来源限流",
        "HttpConnectionError": "网络连接失败",
        "HttpTimeout": "请求超时",
        "timeout": "请求超时",
        "pending": "正文仍待补齐",
        "cancelled": "用户取消",
    }
    return "；".join(
        f"{scopes.get(scope, scope)}：{reasons.get(reason, reason)}"
        for item in failure.split(",")
        for scope, _, reason in (item.partition(":"),)
    )


def _recovery(progress: EvidenceSyncProgress) -> str:
    if progress.component != "complete" or progress.status != "deferred":
        return ""
    result = " | 下次 sync company 从已提交断点续传，已提取正文复用"
    if "disclosure_document_no_text" in progress.failure or "disclosure_document_short_text" in progress.failure:
        result += " | 无文本或短文本公告保持待核实"
    return result


__all__ = ["CompanyEvidenceProgress", "ReportStPreparation"]
