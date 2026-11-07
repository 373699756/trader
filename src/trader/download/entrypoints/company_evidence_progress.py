"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

from trader.infra.progress_format import PadProgressText, ProgressDuration, ShortProgressText

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from math import ceil
from threading import Lock
from typing import TextIO

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage, ProgressNumber, ProgressTask
from trader.download.application.sync_progress import SyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.download.domain.company_evidence import (
    CompanyOperationKind, CompanyOperationProgress, CompanyPublicationProgress, CompanyStockProgress,
    DisclosurePartition, EvidenceSyncProgress,
)
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
                sources=("local",),
            )
        )


def ReportCompanyStock(
    report: Callable[[EvidenceSyncProgress], None],
    names: tuple[tuple[str, str], ...],
    stock: CompanyStockProgress | CompanyPublicationProgress,
) -> None:
    if isinstance(stock, CompanyPublicationProgress):
        report(EvidenceSyncProgress("publish", 0, None, 0, 0, 0, publication=stock,
                                    sources=("local",)))
        return
    name = next((name for code, name in names if code == stock.code), "")
    report(
        EvidenceSyncProgress(
            "facts", stock.processed, stock.total - stock.processed, 0, 0, 0, stock=replace(stock, name=name),
            status="complete" if stock.total and stock.processed == stock.total else "running",
            sources=("local",),
        )
    )


_LABELS = {
    "prepare": "准备资料", "resume": "断点恢复", "st_eligibility": "ST资格", "calendar": "交易日历",
    "financial": "财务", "annual_income": "年度营收", "pledge": "质押", "unlock": "解禁",
    "announcements": "目录", "risk_documents": "正文", "news": "新闻", "facts": "核验",
    "publish": "正式发布", "complete": "结果汇总",
}
_UNITS = {
    "prepare": "项", "resume": "篇", "st_eligibility": "股", "calendar": "份", "financial": "页",
    "annual_income": "组", "pledge": "页", "unlock": "页", "announcements": "范围",
    "risk_documents": "篇", "news": "股", "facts": "股", "publish": "股", "complete": "股",
}
_OPERATIONS: dict[CompanyOperationKind, str] = {
    "prepare": "准备", "inventory": "核对库存", "directory": "目录查询", "download": "下载",
    "text": "文本提取", "ocr": "OCR", "document_commit": "正文入库", "structured": "结构化查询",
    "annual_income": "年度营收", "news": "新闻查询", "publication": "核验发布",
    "collection_commit": "资料入库", "finish": "收尾",
}
_SOURCES = {"cninfo": "巨潮", "eastmoney": "东财", "eastmoney_news": "东财", "eastmoney_annual_income": "东财", "local": "本地"}


def _source_label(sources: tuple[str, ...]) -> str:
    return "/".join(dict.fromkeys(_SOURCES.get(source, source) for source in sources))


def _display_number(value: int | None, width: int = 6) -> str:
    """Keep exact counters when they fit; mark compact quantities as approximate."""
    if value is None:
        return "?".rjust(width)
    exact = str(value)
    if len(exact) <= width:
        return exact.rjust(width)
    exponent = len(exact) - 1
    compact = f"≈{exact[0]}.{exact[1]}e{exponent}"
    if len(compact) > width:
        compact = f"≈{exact[0]}e{exponent}"
    return compact.rjust(width)


class CompanyEvidenceProgress:
    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        report: Callable[[ProgressMessage], None] | None = None,
        observe: Callable[[str, Callable[[], ProgressMessage | None]], None] | None = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._monotonic = monotonic
        if report is None:
            fallback = SyncProgress(TerminalDownloadProgress(self._stream), monotonic)
            fallback.register_observer("company", self.heartbeat_message)
            self._report: Callable[[ProgressMessage], None] = fallback
        else:
            self._report = report
        self._lock = Lock()
        self._stocks: dict[str, CompanyStockProgress] = {}
        self._reported_failures: set[str] = set()
        self._latest: dict[str, EvidenceSyncProgress] = {}
        self._operations: dict[int, CompanyOperationProgress] = {}
        self._changed_codes: set[str] = set()
        self._unpublished_codes: set[str] = set()
        self._last_emitted = float("-inf")
        self._foreground: str | None = None
        self._finished = False
        self._population: int | None = None
        if observe is not None:
            observe("company", self.heartbeat_message)

    def report_preparation(self, message: ProgressMessage) -> None:
        """Flush reference preparation and download handoff feedback under the invoked command."""
        self._report(message)

    def __call__(self, progress: EvidenceSyncProgress) -> None:
        with self._lock:
            if progress.operation is not None:
                event = progress.operation
                if self._finished:
                    return
                if event.state in {"queued", "running"}:
                    if event.token not in self._operations and len(self._operations) >= 160:
                        raise ValueError("company_progress_operation_limit")
                    self._operations[event.token] = event
                else:
                    self._operations.pop(event.token, None)
                return
            messages = self._update(progress)
        for message in messages:
            self._report(message)

    def _update(self, progress: EvidenceSyncProgress) -> tuple[ProgressMessage, ...]:
        if progress.population is not None:
            self._population = progress.population
        if progress.component == "facts" and progress.stock is not None:
            self._stocks[progress.stock.code] = progress.stock
            self._unpublished_codes.add(progress.stock.code)
        previous = self._latest.get(progress.component)
        self._latest[progress.component] = progress
        if progress.publication is not None:
            self._changed_codes.update(progress.publication.changed_codes)
            if progress.publication.state == "committed":
                self._unpublished_codes.difference_update(progress.publication.codes)
        if progress.component == "publish":
            facts = self._latest.get("facts")
            if facts is not None and facts.status == "running":
                self._latest["facts"] = replace(facts, status="complete")
        if progress.component in {"st_eligibility", "calendar"}:
            return (self._message(progress, immediate=True),)
        if progress.component == "resume":
            snapshot = replace(progress, status="complete")
            return (self._message(snapshot, immediate=True), self._summary_message(snapshot))
        self._foreground = progress.component
        new_failures = set(filter(None, progress.failure.split(","))) - self._reported_failures
        self._reported_failures.update(new_failures)
        messages: list[ProgressMessage] = []
        if progress.component != "complete" and (new_failures or progress.failure_document is not None):
            messages.append(self._failure(progress, ",".join(sorted(new_failures)) or progress.failure))
        boundary = (previous is None or previous.status != progress.status
                    or progress.publication is not None and previous.publication != progress.publication)
        if progress.component == "complete":
            self._finished = True
            self._operations.clear()
            boundary = True
        now = self._monotonic()
        if boundary or now - self._last_emitted >= 5:
            starting = progress.status == "running" and (previous is None or previous.status != "running")
            messages.append(self._message(progress, immediate=boundary, starting=starting))
            self._last_emitted = now
        if progress.component == "complete":
            messages.append(self._summary_message(progress))
        if progress.status != "running" and self._foreground == progress.component:
            self._foreground = next((item.component for item in reversed(tuple(self._latest.values()))
                                     if item.component not in {"resume", "complete", "st_eligibility", "calendar"}
                                     and item.status == "running"), None)
        return tuple(messages)

    def heartbeat_message(self) -> ProgressMessage | None:
        """Called by the existing command heartbeat; never touches files or databases."""
        with self._lock:
            now = self._monotonic()
            if self._finished or self._foreground is None or now - self._last_emitted < 5:
                return None
            self._last_emitted = now
            return self._message(self._latest[self._foreground], immediate=False)

    def _message(self, progress: EvidenceSyncProgress, *, immediate: bool, starting: bool = False) -> ProgressMessage:
        if progress.component not in {"st_eligibility", "calendar"}:
            return ProgressMessage(
                tuple(ProgressField(value) for value in self._columns(progress, starting=starting)),
                task=ProgressTask("company", progress.component, _source_label(progress.sources) + _LABELS[progress.component]),
                state="取消" if "task:cancelled" in progress.failure.split(",") else "开始" if starting else "等待" if progress.status == "deferred" else "完成" if progress.status in {"complete", "reused"} else "处理中",
                finished=progress.status != "running",
                immediate=immediate,
                compact=True,
                owner_finished=progress.component == "complete",
            )
        statuses = {"running": "进行中", "complete": "完成", "reused": "本地复用", "deferred": "待续传"}
        status = "complete" if progress.component == "st_eligibility" and progress.pending == 0 else progress.status
        status_text = "开始" if starting else statuses[status]
        failure = progress.failure if progress.component in {"complete", "st_eligibility"} or status == "deferred" else ""
        details = _counts(progress, _UNITS[progress.component]) + f" | 结果 {status_text}" + (f" | 原因 {_failure_text(failure)}" if failure else "")
        return ProgressMessage(
            tuple(ProgressField(part, ProgressLevel.WARNING if failure else ProgressLevel.NORMAL)
                  for part in details.split(" | ")),
            task=ProgressTask(
                "参考" if progress.component in {"st_eligibility", "calendar"} else "company",
                _LABELS[progress.component] if progress.component in {"st_eligibility", "calendar"} else progress.component,
                _source_label(progress.sources) + _LABELS[progress.component],
            ),
            state="开始" if starting else "完成" if status in {"complete", "reused"} else "等待" if status == "deferred" else "处理中",
            finished=status in {"complete", "reused", "deferred"},
            immediate=immediate,
            compact=progress.component not in {"st_eligibility", "calendar"},
        )

    def _columns(self, progress: EvidenceSyncProgress, *, starting: bool) -> tuple[str, str, str, str]:
        running = tuple(item for item in self._operations.values() if item.state == "running")
        queued = sum(item.state == "queued" for item in self._operations.values())
        documents = self._latest.get("risk_documents")
        pending = documents.unpublished_documents if documents is not None else None
        counts = f"下载{sum(item.kind == 'download' for item in running):2} 文本{sum(item.kind == 'text' for item in running):2} OCR{sum(item.kind == 'ocr' for item in running):2} 待发{_display_number(pending)}"
        context = self._context(progress)
        waiting = f"排队{queued:2} 队列估算中" if queued else "暂无活动操作"
        if running:
            oldest = min(running, key=lambda item: item.at)
            if progress.publication is None or progress.publication.state != "committed":
                context = self._operation_context(oldest) if oldest.code or progress.scope is None else self._context(progress)
            waiting = f"操作等待{ProgressDuration(self._monotonic() - oldest.at)}"
        elif queued:
            oldest = min(self._operations.values(), key=lambda item: item.at)
            context = self._operation_context(oldest)
            waiting = f"排队等待{ProgressDuration(self._monotonic() - oldest.at)}"
        if documents is not None:
            if documents.discovering_documents:
                waiting += " 目录仍增长"
            elif documents.deferred_documents:
                waiting += f" 冷却{_display_number(documents.deferred_documents).strip()}篇"
            elif documents.document_eta_seconds is not None:
                waiting += f" 队列约{ProgressDuration(documents.document_eta_seconds)}"
            elif documents.unresolved_document_failures and not running and not queued:
                waiting = f"未解决失败{documents.unresolved_document_failures}篇"
        if progress.status == "reused":
            waiting = "本地复用原来源缓存"
        elif progress.status == "deferred":
            waiting = "待续传"
        elif progress.status == "complete":
            waiting = "本批结束" if progress.component == "risk_documents" else "核验结束，尚未发布" if progress.component == "facts" else "正式提交完成" if progress.component == "publish" else "阶段完成"
        elif starting and not self._operations and waiting == "暂无活动操作":
            waiting = "开始，等待阶段操作"
        if progress.component == "news" and progress.completed == progress.pending == 0:
            waiting = "无候选，本轮未覆盖"
        if progress.component == "prepare" and progress.status == "running":
            waiting = "东财=东方财富 巨潮=巨潮资讯"
        elif progress.component == "complete":
            counts = f"目录待补{_display_number(progress.pending)} 正文{_display_number(progress.pending_documents)}"
            context = f"本轮变更{_display_number(len(self._changed_codes))}股（去重）"
            waiting = "待处理数量未核对" if progress.pending is None or progress.pending_documents is None else "待续传" if progress.status == "deferred" else "完成（含待核实）"
        return (PadProgressText(self._count_column(progress), 24), PadProgressText(context, 34),
                PadProgressText(counts, 32), PadProgressText(waiting, 32))

    def _count_column(self, progress: EvidenceSyncProgress) -> str:
        completed = _display_number(progress.completed)
        total = _display_number(None if progress.pending is None else progress.completed + progress.pending)
        if progress.component in {"financial", "pledge", "unlock"}:
            return f"持久页{completed}/{total}"
        if progress.component == "announcements":
            dates = progress.dates
            return "规划目录日期" if dates is None else f"全市场日{_display_number(dates.completed_days)}/{_display_number(dates.total_days)}"
        if progress.component == "risk_documents":
            due = (max(0, progress.pending - progress.deferred_documents - progress.unextractable_documents)
                   if progress.pending is not None and progress.deferred_documents is not None
                   and progress.unextractable_documents is not None else None)
            return f"已入库{_display_number(progress.completed, 7)} 待{_display_number(due)}篇"
        if progress.component == "facts":
            return f"核验{completed}/{total}股"
        if progress.component == "publish" and progress.publication is not None:
            return f"本批{'提交' if progress.publication.state == 'committed' else '待发'}{_display_number(len(progress.publication.codes))}股"
        if progress.component in {"publish", "complete"}:
            return f"正式覆盖{completed}/{_display_number(self._population)}股"
        if progress.component == "news":
            return "无候选，未抓取新闻" if progress.completed == progress.pending == 0 else f"处理{completed}/{total}股"
        if progress.component == "annual_income":
            return f"持久组{completed}/{total}"
        return "断点库存快照" if progress.component == "resume" else "公司资料准备"

    def _context(self, progress: EvidenceSyncProgress) -> str:
        scope = progress.scope
        if isinstance(scope, DisclosurePartition):
            market = {"sse": "上交所", "szse": "深交所", "bse": "北交所"}[scope.market]
            dates = scope.start.strftime("%m-%d") + ("~" + scope.end.strftime("%m-%d") if scope.start != scope.end else "")
            pages = None if scope.expected_rows is None or scope.expected_rows > 3000 else ceil(scope.expected_rows / scope.page_size) or 1
            return f"{market} {dates} {_display_number(scope.page)}/{_display_number(pages)}页"
        if scope is not None:
            return str(scope.start) if scope.start == scope.end else f"{scope.start}~{scope.end}"
        if progress.stock is not None:
            stock = progress.stock
            return f"{stock.code}-{ShortProgressText(stock.name, 12)}" + (" 核验未发布" if progress.component == "facts" else "")
        if progress.publication is not None:
            return "发布收尾（本批已提交）" if progress.publication.state == "committed" else "正式提交中，尚未发布"
        return "本地复用" if progress.status == "reused" else "本地资料" if progress.sources == ("local",) else "等待阶段操作"

    def _operation_context(self, operation: CompanyOperationProgress) -> str:
        kind = {"download": "下载", "text": "文本", "ocr": "OCR", "document_commit": "入库", "annual_income": "营收", "news": "新闻"}.get(operation.kind)
        if operation.code and kind is not None:
            source = ShortProgressText(_source_label((operation.source,)), 4)
            return f"{source}{kind} {operation.code}-{ShortProgressText(operation.name, 12)} {ShortProgressText(operation.title, 4)}"
        return _OPERATIONS[operation.kind] + "（操作最久）"

    def _summary_message(self, progress: EvidenceSyncProgress) -> ProgressMessage:
        details = _counts(progress, _UNITS[progress.component]) + _coverage(progress)
        if progress.component == "complete":
            details += self._summary()
            documents = self._latest.get("risk_documents")
            if documents is not None:
                unresolved = progress.workload.unresolved_failures if progress.workload is not None else None
                details += f" | 正文入库 {documents.completed} 篇 | 本轮失败 {documents.failed_documents} 篇 | 未解决失败 {unresolved if unresolved is not None else '未核对'} 篇"
            details += f" | 冷却 {progress.deferred_documents if progress.deferred_documents is not None else '未核对'} 篇 | OCR分类 {progress.ocr_documents if progress.ocr_documents is not None else '未核对'} 篇"
            details += f" | 本轮成功页 {progress.fetched_pages} | 原始资料变更 {progress.changed_rows} 条（非每股发布行） | 缓存复用 {progress.reused_partitions} 次"
            details += (f" | 原因 {_failure_text(progress.failure)}" if progress.failure else "") + _recovery(progress)
        return ProgressMessage(tuple(ProgressField(part) for part in details.split(" | ")), task=ProgressTask("company", progress.component + "_summary", "本地库存统计" if progress.component == "resume" else "本地结果统计"),
                               state="完成", finished=True, immediate=True, compact=True)

    def _failure(self, progress: EvidenceSyncProgress, failure: str) -> ProgressMessage:
        reason = f"risk_documents:{progress.failure_reason}" if progress.failure_reason is not None else failure
        parts = [f"原因 {_failure_text(reason)}"]
        document = progress.failure_document
        if document is not None:
            stock = progress.stock
            name = stock.name if stock is not None and stock.code == document.codes[0] else ""
            parts.insert(0, f"{document.codes[0]} {name} {ShortProgressText(document.title, 48)}")
            parts.append(f"公告ID {document.source}:{document.identity} 修订 {document.digest[:12]}")
        if progress.failure_stage is not None:
            parts.insert(0, f"操作 {_OPERATIONS[progress.failure_stage]}")
        sources = (document.source,) if document is not None else progress.sources
        return ProgressMessage(tuple(ProgressField(part, ProgressLevel.ERROR) for part in parts),
                               task=ProgressTask("company", "failure", _source_label(sources) + "失败"), state="失败", finished=True,
                               immediate=True, compact=True)

    def _summary(self) -> str:
        if not self._stocks:
            return f" | 本轮变更股票 {len(self._changed_codes)} 股（去重）"
        stocks = tuple(self._stocks.values())
        complete = sum(stock.coverage_complete for stock in stocks)
        reused = sum(stock.reused for stock in stocks)
        return (
            f" | 本轮变更股票 {len(self._changed_codes)} 股（去重）"
            f" | 已核验 {len(stocks)} 股 | 完整 {complete} 股 | 部分 {len(stocks) - complete} 股"
            f" | 本轮待发布 {len(self._unpublished_codes)} 股 | 编译变更 {len(stocks) - reused} 股 | 复用 {reused} 股"
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
    if progress.component == "prepare":
        return "准备公司资料和已发布输入" if progress.status == "running" else "公司资料准备完成"
    if progress.component == "resume" and progress.workload is not None:
        work = progress.workload
        return (
            f"已发布输入 {work.published_inputs} 篇 | 有效替代 {work.superseded_inputs} 篇 | "
            f"已分析待发布 {work.analyzed_pending} 篇 | 本地待分析 {work.local_pending} 篇 | "
            f"需下载 {work.download_pending} 篇（含冷却） | 冷却 {work.cooling} 篇 | "
            f"不可提取/资料未知 {work.unextractable} 篇 | 未解决失败 {work.unresolved_failures} 篇 | raw占用 {work.raw_bytes / 1024**2:.1f} MiB"
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
        return "无需请求，本地复用有效缓存（保留原来源）"
    if progress.pending is None:
        return f"进度 {progress.completed}/? {unit}（总数待确认）"
    total = progress.completed + progress.pending
    return f"已持久化 {ProgressNumber(progress.completed)}/{ProgressNumber(total)} {unit}"


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


__all__ = ["CompanyEvidenceProgress", "ReportCompanyStock", "ReportStPreparation"]
