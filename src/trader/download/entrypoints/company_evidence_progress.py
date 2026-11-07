"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

from trader.infra.progress_format import ProgressDuration

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from math import ceil
from typing import TextIO

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage, ProgressTask
from trader.download.application.sync_progress import SyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress
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
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        report: Callable[[ProgressMessage], None] | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._report = report or SyncProgress(TerminalDownloadProgress(self._stream), monotonic)
        self._stocks: dict[str, CompanyStockProgress] = {}
        self._batch = 0
        self._reported_failures: set[str] = set()

    def report_preparation(self, message: ProgressMessage) -> None:
        """Flush reference preparation and download handoff feedback under the invoked command."""
        self._report(message)

    def __call__(self, progress: EvidenceSyncProgress) -> None:
        self._batch = progress.batch or self._batch
        if progress.component == "facts" and progress.stock is not None:
            self._stocks[progress.stock.code] = progress.stock
        labels = {
            "resume": "断点恢复",
            "st_eligibility": "ST资格",
            "calendar": "交易日历",
            "financial": "财务增量下载",
            "annual_income": "合同年度营收补取",
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
            "resume": "篇",
            "st_eligibility": "股",
            "calendar": "份",
            "financial": "页",
            "annual_income": "组",
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
        if progress.component == "risk_documents" and status == "complete":
            status_text = "本批结束"
        if progress.component == "news" and progress.completed == progress.pending == 0:
            status_text = "未覆盖"
        failure = progress.failure
        if progress.component != "complete" and progress.status == "running":
            current_failures = {item for item in failure.split(",") if item}
            failure = ",".join(sorted(current_failures - self._reported_failures))
            self._reported_failures.update(current_failures)
        details = (
            f"{_counts(progress, units[progress.component])}"
            + (f" | 批次 {self._batch}" if self._batch and progress.component != "complete" else "")
            + _scope(progress)
            + _coverage(progress)
            + _stock(progress)
            + (self._summary(progress.completed) if progress.component == "complete" else "")
            + (" | 补查历史发行人（保留已完成页）" if progress.activity == "repair" else "")
            + (
                f" | 本轮成功页 {progress.fetched_pages} | 原始资料变更 {progress.changed_rows} 条（非每股发布行）"
                f" | 本轮缓存复用 {progress.reused_partitions} 次"
                if progress.component == "complete"
                else ""
            )
            + f" | 结果 {status_text}" + (f" | 原因 {_failure_text(failure)}" if failure else "") + _recovery(progress)
        )
        self._report(ProgressMessage(
            tuple(ProgressField(part, ProgressLevel.WARNING if failure else ProgressLevel.NORMAL)
                  for part in details.split(" | ")),
            task=ProgressTask(
                "参考" if progress.component in {"st_eligibility", "calendar"} else "company",
                labels[progress.component] if progress.component in {"st_eligibility", "calendar"} else progress.component,
                labels[progress.component], self._batch,
            ),
            state="完成" if status in {"complete", "reused"} else "等待" if status == "deferred" else "处理中",
            finished=status in {"complete", "reused", "deferred"},
        ))

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


def _coverage(progress: EvidenceSyncProgress) -> str:
    if progress.workload is None:
        return ""
    return "".join(
        f" | {item.market}目录D(日) {item.discovered_day or '未闭合'} / 处理P(日) {item.processed_day or '未闭合'}"
        for item in progress.workload.watermarks
    )


def _counts(progress: EvidenceSyncProgress, unit: str) -> str:
    if progress.component == "resume" and progress.workload is not None:
        work = progress.workload
        return (
            f"已发布输入 {work.published_inputs} 篇 | 有效替代 {work.superseded_inputs} 篇 | "
            f"已分析待发布 {work.analyzed_pending} 篇 | 本地待分析 {work.local_pending} 篇 | "
            f"需下载 {work.download_pending} 篇（含冷却） | 冷却 {work.cooling} 篇 | "
            f"不可提取/资料未知 {work.unextractable} 篇 | raw占用 {work.raw_bytes / 1024**2:.1f} MiB"
        )
    if progress.component == "complete":
        partitions = f"公告待补 {progress.pending} 个范围" if progress.pending is not None else "公告待补数量未核对"
        documents = f"可重试正文 {progress.pending_documents} 篇" if progress.pending_documents is not None else "可重试正文数量未核对"
        unextractable = (
            f"不可提取/资料未知 {progress.unextractable_documents} 篇"
            if progress.unextractable_documents is not None else "不可提取/资料未知数量未核对"
        )
        return (
            f"已发布 {progress.completed} 股（含待核实） | {partitions} | {documents} | {unextractable}"
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
            + (
                f" | 已知剩余 {progress.known_pages} 页 | 页数待确认 {progress.unknown_queries} 个查询"
                if progress.batch
                else ""
            )
        )
    if progress.component == "news" and progress.completed == progress.pending == 0:
        return "无候选名单，本轮未抓取新闻"
    if progress.pending is None:
        return f"进度 {progress.completed}/? {unit}（总数待确认）"
    if progress.component == "facts" and progress.stock is not None:
        return f"本批已核验股票 {progress.stock.processed}/{progress.stock.total} 股（等待原子发布）"
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
        if progress.deferred_documents is None or progress.unextractable_documents is None or progress.ocr_documents is None:
            return f"已尝试 {progress.attempted_documents} 篇 | 已提取正文 {progress.completed} 篇 | 正文待处理数量未核对"
        return (
            f"已尝试 {progress.attempted_documents}/{total} 篇 | 已提取正文 {progress.completed} 篇 | "
            f"本轮失败 {progress.failed_documents} 篇 | 未解决失败 {progress.unresolved_document_failures} 篇 | 当前可处理 "
            f"{max(0, pending - progress.deferred_documents - progress.unextractable_documents)} 篇 | "
            f"冷却暂缓 {progress.deferred_documents} 篇 | OCR分类 {progress.ocr_documents} 篇 | "
            f"不可提取/资料未知 {progress.unextractable_documents} 篇"
            + (
                f" | 提取速度 {progress.document_rate * 60:.1f} 篇/分"
                f" | 当前队列预计 {ProgressDuration(progress.document_eta_seconds)}"
                if progress.document_rate is not None and progress.document_eta_seconds is not None
                else ""
            )
            + (
                f" | 最早重试 {progress.next_document_retry_at.isoformat(timespec='seconds')}"
                if progress.next_document_retry_at is not None
                else ""
            )
            + (" | 公告目录仍在发现新正文" if progress.discovering_documents else "")
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
            f"/{ceil(scope.expected_rows / scope.page_size) or 1}"
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
        "disclosure_document_oversized": "PDF超过扩展大小上限",
        "disclosure_document_page_limit": "PDF超过页数上限",
        "disclosure_document_expanded_page_limit": "PDF超过扩展页数上限",
        "disclosure_document_text_limit": "PDF文字超过扩展上限",
        "disclosure_document_encrypted": "PDF已加密",
        "disclosure_document_ocr_unavailable": "本机OCR工具不可用",
        "disclosure_document_ocr_language_unavailable": "OCR中文语言包不可用",
        "disclosure_document_ocr_timeout": "OCR超时",
        "disclosure_document_ocr_timeout_exhausted": "OCR两次超时，自动重试已用尽",
        "disclosure_document_ocr_unreadable": "OCR无法读取PDF",
        "disclosure_document_ocr_low_quality": "OCR文字或置信度不足",
        "disclosure_document_ocr_page_limit": "PDF超过OCR页数上限",
        "disclosure_document_ocr_resource_limit": "OCR临时资源超过上限",
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
    if "task:cancelled" in progress.failure.split(","):
        result += " | 中断后未重新核对待处理数，不表示无剩余任务"
    if "disclosure_document_no_text" in progress.failure or "disclosure_document_short_text" in progress.failure:
        result += " | OCR仍失败的正文标记为不可提取/资料未知，同内容同策略不再重试"
    return result


__all__ = ["CompanyEvidenceProgress", "ReportStPreparation"]
