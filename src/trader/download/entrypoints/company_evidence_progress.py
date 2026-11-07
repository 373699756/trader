"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

from trader.infra.progress_format import ProgressDuration, ShortProgressText

import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from math import ceil
from threading import Lock
from typing import Literal, TextIO

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage, ProgressNumber, ProgressRow, ProgressTask
from trader.download.application.sync_progress import SyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.download.domain.company_evidence import (
    CompanyOperationKind, CompanyOperationProgress, CompanyPublicationProgress, CompanyStockProgress,
    DisclosurePartition, EvidenceSyncProgress,
)
from trader.download.domain.history_sync import HistoryStEligibilitySummary
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.download_failure import DownloadFailureObservation
from trader.download.entrypoints.company_result_feedback import CompanyResultFeedback


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
                                    sources=("local",), completion_scope="batch"))
        return
    name = next((name for code, name in names if code == stock.code), "")
    report(
        EvidenceSyncProgress(
            "facts", stock.processed, stock.total - stock.processed, 0, 0, 0, stock=replace(stock, name=name),
            status="complete" if stock.total and stock.processed == stock.total else "running",
            sources=("local",),
            completion_scope="batch",
        )
    )


def ReportCompanyFailure(
    report: Callable[[EvidenceSyncProgress], None] | None, observation: DownloadFailureObservation,
) -> None:
    if report is not None:
        report(EvidenceSyncProgress("diagnostic", 0, None, 0, 0, 0, diagnostic=observation))


_LABELS = {
    "prepare": "准备资料", "resume": "断点恢复", "st_eligibility": "ST资格", "calendar": "交易日历",
    "financial": "财务", "annual_income": "年度营收", "pledge": "质押", "unlock": "解禁",
    "announcements": "目录", "risk_documents": "正文", "news": "新闻", "facts": "核验",
    "publish": "正式发布", "finish": "收尾", "complete": "结果汇总",
}
_UNITS = {
    "prepare": "项", "resume": "篇", "st_eligibility": "股", "calendar": "份", "financial": "页",
    "annual_income": "组", "pledge": "页", "unlock": "页", "announcements": "范围",
    "risk_documents": "篇", "news": "股", "facts": "股", "publish": "股", "finish": "项", "complete": "股",
}
_OPERATIONS: dict[CompanyOperationKind, str] = {
    "prepare": "准备", "inventory": "核对库存", "directory": "目录查询", "download": "下载",
    "text": "文本提取", "ocr": "OCR", "document_commit": "正文入库", "structured": "结构化查询",
    "annual_income": "年度营收", "news": "新闻查询", "publication": "核验发布",
    "collection_commit": "资料入库", "finish": "收尾",
}
_SOURCES = {"cninfo": "巨潮", "eastmoney": "东财", "eastmoney_news": "东财", "eastmoney_annual_income": "东财", "local": "本地"}
_TASK_GROUPS = (
    ("prepare", "resume"), ("financial",), ("pledge",), ("unlock",), ("announcements",),
    ("risk_documents",), ("facts", "publish"), ("annual_income", "news"), ("finish",),
)
_GROUP_LABELS = ("准备恢复", "财务", "质押", "解禁", "目录", "正文", "核验发布", "营收新闻", "收尾")


def _source_label(sources: tuple[str, ...]) -> str:
    return "/".join(dict.fromkeys(_SOURCES.get(source, source) for source in sources))


def _quantity(value: int | None) -> str:
    return "未核对" if value is None else str(value)


def _market_label(market: str) -> str:
    return {"sse": "上交所", "szse": "深交所", "bse": "北交所"}[market]


def _data_state(state: CompanyDataState | None) -> str:
    return "待核验" if state is None else {
        CompanyDataState.COMPLETE: "完整", CompanyDataState.PARTIAL: "部分", CompanyDataState.UNAVAILABLE: "缺失",
    }[state]


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
        self._output_lock = Lock()
        self._feedback = CompanyResultFeedback()
        self._stocks: dict[str, CompanyStockProgress] = {}
        self._reported_failures: set[str] = set()
        self._latest: dict[str, EvidenceSyncProgress] = {}
        self._operations: dict[int, CompanyOperationProgress] = {}
        self._changed_codes: set[str] = set()
        self._unpublished_codes: set[str] = set()
        self._received_codes: set[str] = set()
        self._committed_codes: set[str] = set()
        self._waiting_codes: set[str] = set()
        self._component_codes: dict[str, set[str]] = {}
        self._query_bases: dict[tuple[str, date, date], int] = {}
        self._last_persisted: float | None = None
        self._original_coverage: int | None = None
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
            if self._finished:
                return
            self._feedback.observe_run(progress.run_id)
            if progress.diagnostic is not None:
                messages = self._feedback.diagnostic(progress.diagnostic, self._stocks)
            elif progress.result is not None:
                result = progress.result
                messages = self._feedback.result(result)
                if messages and result.outcome == "downloaded":
                    self._received_codes.update(result.related_codes)
                if messages and result.outcome in {"accepted", "empty"} and result.commit_operation_id is not None:
                    self._last_persisted = self._monotonic()
            elif progress.recovery is not None:
                messages = self._feedback.recovery(progress.recovery)
            elif progress.operation is not None:
                event = progress.operation
                if event.state in {"queued", "running"}:
                    if event.token not in self._operations and len(self._operations) >= 160:
                        raise ValueError("company_progress_operation_limit")
                    self._operations[event.token] = event
                else:
                    self._operations.pop(event.token, None)
                return
            else:
                messages = self._update(progress)
        for message in messages:
            with self._output_lock:
                with self._lock:
                    if self._finished and progress.component != "complete":
                        return
                self._report(message)

    def _update(self, progress: EvidenceSyncProgress) -> tuple[ProgressMessage, ...]:
        if progress.publication is not None and not self._feedback.is_new_publication(progress.publication):
            return ()
        if progress.component == "resume":
            progress = replace(progress, status="complete")
        if progress.population is not None:
            self._population = progress.population
        if progress.existing_published_codes is not None:
            self._original_coverage = progress.existing_published_codes
        if progress.component == "facts" and progress.stock is not None:
            self._stocks[progress.stock.code] = progress.stock
            self._unpublished_codes.add(progress.stock.code)
            if progress.stock.coverage_complete and progress.stock.pending == 0:
                self._waiting_codes.discard(progress.stock.code)
            else:
                self._waiting_codes.add(progress.stock.code)
        previous = self._latest.get(progress.component)
        self._latest[progress.component] = progress
        structured = progress.structured
        if structured is not None:
            codes = self._component_codes.setdefault(progress.component, set())
            if structured.restarted:
                codes.clear()
            codes.update(structured.qualified_codes)
            self._received_codes.update(structured.qualified_codes)
            query = structured.query
            key = (progress.component, query.start, query.end)
            if key not in self._query_bases or structured.restarted:
                self._query_bases[key] = max(0, progress.completed - (1 if structured.page_rows else 0))
        if progress.component in {"financial", "pledge", "unlock", "announcements", "risk_documents"}:
            if previous is not None and progress.completed > previous.completed:
                if structured is None or progress.completed == structured.query.page:
                    self._last_persisted = self._monotonic()
        if progress.publication is not None:
            if progress.publication.state == "committed":
                self._changed_codes.update(progress.publication.changed_codes)
                self._unpublished_codes.difference_update(progress.publication.codes)
                self._committed_codes.update(progress.publication.codes)
                self._last_persisted = self._monotonic()
        if progress.component == "publish":
            facts = self._latest.get("facts")
            if facts is not None and facts.status == "running":
                self._latest["facts"] = replace(facts, status="complete")
        if progress.component in {"st_eligibility", "calendar"}:
            return (self._message(progress, immediate=True),)
        if progress.component == "resume":
            self._last_emitted = self._monotonic()
            return (self._message(progress, immediate=True), self._summary_message(progress))
        self._foreground = progress.component
        new_failures = set(filter(None, progress.failure.split(","))) - self._reported_failures
        self._reported_failures.update(new_failures)
        messages: list[ProgressMessage] = []
        if (progress.component in {"financial", "pledge", "unlock", "announcements", "news"}
                and progress.completion_scope == "component" and progress.status == "complete" and progress.pending == 0
                and (progress.component != "announcements" or progress.dates is not None
                     and progress.dates.completed_days == progress.dates.total_days)):
            messages.extend(self._feedback.confirm_component(progress.component))
        reason = progress.failure_reason or next(iter(new_failures), "").partition(":")[2]
        document = progress.failure_document
        diagnosed = self._feedback.diagnosed(
            progress.component, reason, f"{document.source}:{document.identity}" if document is not None else "",
            document.digest if document is not None else "",
        )
        if progress.component != "complete" and not diagnosed and (new_failures or document is not None):
            messages.append(self._failure(progress, ",".join(sorted(new_failures)) or progress.failure))
        if progress.publication is not None:
            publishing = tuple(item.token for item in self._operations.values()
                               if item.kind == "publication" and item.state == "running")
            messages.extend(self._feedback.publication(
                progress.publication, self._stocks, operation_id=publishing[0] if len(publishing) == 1 else None,
            ))
        boundary = (previous is None
                    or progress.completion_scope == "component" and previous.status != progress.status
                    or progress.publication is not None and progress.publication.state == "committed")
        if progress.component == "complete":
            self._finished = True
            self._operations.clear()
            boundary = True
            messages.extend(self._feedback.conclude_remaining(incomplete=progress.status == "deferred"))
        elif progress.completion_scope == "component" and progress.status in {"complete", "reused", "deferred"}:
            messages.extend(self._feedback.conclude(
                progress.component, incomplete=progress.status == "deferred" or bool(progress.pending),
            ))
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
                tuple(ProgressField(value) for value in self._overview(progress)),
                task=ProgressTask("company", progress.component, "总览"),
                state=_terminal_result(progress)[1] if progress.component == "complete" else "开始" if starting else "等待" if progress.status == "deferred" else "完成" if progress.status in {"complete", "reused"} else "处理中",
                finished=progress.status != "running",
                immediate=immediate,
                compact=True,
                owner_finished=progress.component == "complete",
                heartbeat=False,
                rows=(
                    ProgressRow("范围", tuple(ProgressField(value) for value in self._scope_fields(progress))),
                    ProgressRow("任务", tuple(ProgressField(value) for value in self._task_fields(progress, starting=starting))),
                ),
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

    def _group_state(self, group: tuple[str, ...]) -> str:
        events = tuple(self._latest[key] for key in group if key in self._latest)
        if not events:
            return "待处理"
        if any(item.status == "deferred" for item in events):
            return "待续传"
        terminal = self._latest.get("complete")
        unfinished = any(item.status == "running" or item.completion_scope == "batch" for item in events)
        if terminal is not None:
            if terminal.status == "deferred" or terminal.pending is None:
                return "待续传" if unfinished else "完成"
            return "完成"
        if group == _TASK_GROUPS[0] and "resume" not in self._latest:
            return "进行"
        if unfinished:
            return "进行"
        return "完成"

    def _overview(self, progress: EvidenceSyncProgress) -> tuple[str, ...]:
        states = tuple(self._group_state(group) for group in _TASK_GROUPS)
        parts = ["事项9项：" + "、".join(f"{label}{states.count(label)}" for label in ("完成", "进行", "待处理", "待续传"))]
        parts.append(f"本轮已获取{len(self._received_codes)}股、核验{len(self._stocks)}/{_quantity(self._population)}股、提交{len(self._committed_codes)}股（去重）")
        parts.append(f"已知待补{len(self._waiting_codes)}股；未核验{_quantity(None if self._population is None else max(0, self._population - len(self._stocks)))}股")
        parts.append(f"当前{_LABELS[progress.component]}；来源{_source_label(progress.sources) or '未提供'}")
        if self._feedback.has_diagnostics:
            parts.extend(self._feedback.summary()[:1])
        if progress.component in {"prepare", "resume", "complete"} and self._original_coverage is not None:
            parts.append(f"开场已有正式覆盖{self._original_coverage}股（不计本轮提交）")
        if progress.component == "complete":
            parts.append(_terminal_result(progress)[0])
        else:
            pending = "/".join(label for label, state in zip(_GROUP_LABELS, states, strict=True) if state == "待处理")
            if pending:
                parts.append("待办" + pending)
        return tuple(parts)

    def _oldest_operation(self) -> CompanyOperationProgress | None:
        running = tuple(item for item in self._operations.values() if item.state == "running")
        return min(running or tuple(self._operations.values()), key=lambda item: item.at, default=None)

    def _scope_fields(self, progress: EvidenceSyncProgress) -> tuple[str, ...]:
        parts: list[str] = []
        scope = progress.scope
        if scope is not None:
            meaning = {"financial": "披露窗口", "pledge": "所选质押快照日", "unlock": "未来解禁窗口", "announcements": "当前目录日期"}.get(progress.component, "日期窗口")
            parts.append(f"{meaning}{scope.start}～{scope.end}")
            if progress.component == "pledge":
                parts.append("快照查找限定本轮截止日前365天内")
        structured = progress.structured
        if structured is not None:
            if structured.first_date is not None:
                parts.append(f"本页资料日期{structured.first_date}～{structured.last_date}")
            parts.append(f"合格股票已获取{len(self._component_codes.get(progress.component, set()))}/{_quantity(self._population)}股（本查询已见，非覆盖）")
            parts.append("批量接口，无单一当前股票")
        if isinstance(scope, DisclosurePartition):
            pages = None if scope.expected_rows is None or scope.expected_rows > 3000 else max(1, ceil(scope.expected_rows / scope.page_size))
            parts.append(f"{_market_label(scope.market)} 当前第{scope.page}/{pages if pages is not None else '待发现'}页")
        directory = progress if progress.component == "announcements" else self._latest.get("announcements")
        if directory is not None:
            dates = directory.dates
            parts.append("全市场目录日期待规划" if dates is None else f"目录窗口{dates.window_start}～{dates.window_end}；全市场日期闭合{dates.completed_days}/{dates.total_days}天")
            parts.extend(f"{_market_label(market)}{dates.completed_days}/{dates.total_days}天" for market, dates in directory.market_dates)
        operation = self._oldest_operation()
        stock = progress.stock
        if operation is not None and operation.code:
            stock = self._stocks.get(operation.code)
            parts.append(f"当前股票{operation.code}-{ShortProgressText(operation.name, 16)}；{ShortProgressText(operation.title, 36)}")
            if stock is None:
                parts.append("股票资料：财务待核验、质押待核验、解禁待核验、公告待核验")
        elif stock is not None and progress.component != "announcements":
            parts.append(f"当前股票{stock.code}-{ShortProgressText(stock.name, 16)}")
        if stock is not None and progress.component != "announcements":
            parts.append("股票资料：" + "、".join(
                f"{label}{_data_state(state)}" for label, state in (
                    ("财务", stock.financial_state), ("质押", stock.pledge_state),
                    ("解禁", stock.unlock_state), ("公告", stock.announcement_state),
                )
            ))
            parts.append(f"该股正文已分析{stock.analyzed}/{stock.announcements}篇，待补{stock.pending}篇")
            if stock.financial_report_date is not None:
                parts.append(f"财务报告期{stock.financial_report_date}，披露日{stock.financial_disclosed_at.date() if stock.financial_disclosed_at else '未核对'}")
            if stock.pledge_date is not None:
                parts.append(f"质押快照日{stock.pledge_date}")
            if stock.unlock_coverage_end is not None:
                parts.append(f"解禁核对至{stock.unlock_coverage_end}")
            parts.append(f"风险{stock.risks}条、利好{stock.positives}条、待核实{stock.review_count}项")
        parts.append(f"资料来源{_source_label(progress.sources) or '未提供'}")
        if progress.status == "reused":
            parts.append("本地复用原来源缓存")
        elif progress.sources == ("local",):
            parts.append("本地处理")
        return tuple(parts)

    def _task_fields(self, progress: EvidenceSyncProgress, *, starting: bool) -> tuple[str, ...]:
        parts = [self._task_count(progress), f"资料来源{_source_label(progress.sources) or '未提供'}"]
        structured = progress.structured
        if structured is not None:
            key = (progress.component, structured.query.start, structured.query.end)
            baseline = self._query_bases[key]
            parts.append(f"本次续传新保存{max(0, progress.completed - baseline)}页，当前查询累计{structured.persisted_rows}原始行")
            parts.append("查询重开，原页进度已清零" if structured.restarted else "接续已保存分页" if structured.resumed else "当前查询")
        operation = self._oldest_operation()
        if operation is not None:
            parts.append(f"{'排队等待' if operation.state == 'queued' else '正在'}{_OPERATIONS[operation.kind]}；来源{_source_label((operation.source,))}")
            parts.append(f"{'排队等待' if operation.state == 'queued' else '本操作'}{ProgressDuration(self._monotonic() - operation.at)}")
            if operation.completed_units is not None:
                parts.append(f"OCR已处理{operation.completed_units}/{operation.total_units}页（含有效页缓存）")
        elif starting:
            parts.append("开始，等待阶段操作")
        if progress.publication is not None:
            parts.append("发布收尾（本批已提交）" if progress.publication.state == "committed" else "正式提交中，尚未发布")
        elif progress.component == "facts":
            parts.append("核验未发布")
        elif progress.status == "deferred":
            parts.append("待续传")
        elif progress.status == "complete":
            parts.append("本批结束" if progress.completion_scope == "batch" else "阶段完成")
        if self._last_persisted is not None:
            parts.append(f"距持久推进{ProgressDuration(self._monotonic() - self._last_persisted)}")
        else:
            parts.append("本轮尚无新的持久推进")
        documents = self._latest.get("risk_documents")
        if documents is not None:
            running = tuple(item for item in self._operations.values() if item.state == "running")
            parts.append(f"执行中：下载{sum(item.kind == 'download' for item in running)}、文本{sum(item.kind == 'text' for item in running)}、OCR{sum(item.kind == 'ocr' for item in running)}；排队{sum(item.state == 'queued' for item in self._operations.values())}")
            parts.append(f"正文冷却{_quantity(documents.deferred_documents)}篇、不可提取{_quantity(documents.unextractable_documents)}篇、未解决失败{documents.unresolved_document_failures}篇、已分析待发{_quantity(documents.unpublished_documents)}篇")
            if documents.discovering_documents:
                parts.append("目录仍增长，正文总数待发现")
            elif documents.document_eta_seconds is not None:
                parts.append(f"当前可处理正文队列约{ProgressDuration(documents.document_eta_seconds)}")
            if documents.next_document_retry_at is not None:
                parts.append(f"最早重试{documents.next_document_retry_at.isoformat(timespec='seconds')}")
        else:
            parts.append("正文处理尚未开始")
        return tuple(parts)

    def _task_count(self, progress: EvidenceSyncProgress) -> str:
        total = "待发现" if progress.pending is None else str(progress.completed + progress.pending)
        if progress.status == "reused":
            return f"{_LABELS[progress.component]}复用有效缓存；本轮网络0页" if progress.component in {"financial", "pledge", "unlock"} else f"{_LABELS[progress.component]}复用有效缓存"
        if progress.component in {"financial", "pledge", "unlock"}:
            return f"{_LABELS[progress.component]}已保存{progress.completed}/{total}页" + (f"，{progress.completed / (progress.completed + progress.pending) * 100:.1f}%" if progress.pending is not None and progress.completed + progress.pending else "")
        if progress.component == "risk_documents":
            due = max(0, progress.pending - progress.deferred_documents - progress.unextractable_documents) if progress.pending is not None and progress.deferred_documents is not None and progress.unextractable_documents is not None else None
            return f"正文已入库{progress.completed}篇，可处理待补{_quantity(due)}篇；本轮失败{progress.failed_documents}篇"
        if progress.component == "facts":
            return f"本批核验{progress.completed}/{total}股；本轮去重核验{len(self._stocks)}股"
        if progress.publication is not None:
            return f"本批{'提交' if progress.publication.state == 'committed' else '待发'}{len(progress.publication.codes)}股；本轮去重提交{len(self._committed_codes)}股"
        if progress.component == "publish":
            return f"正式覆盖{progress.completed}/{_quantity(self._population)}股；本轮提交{len(self._committed_codes)}股"
        if progress.component == "announcements":
            return f"目录已持久推进{progress.completed}个范围，已知待补{_quantity(progress.pending)}个范围（可继续细分）；已知待取{progress.known_pages}页、待发现页数的查询{progress.unknown_queries}个"
        if progress.component == "news" and progress.completed == progress.pending == 0:
            return "无候选，未抓取新闻；本轮未覆盖"
        if progress.component == "complete":
            return f"目录待补{_quantity(progress.pending)}个范围；正文待补{_quantity(progress.pending_documents)}篇；本轮变更{len(self._changed_codes)}股（去重）"
        return f"{_LABELS[progress.component]}{progress.completed}/{total}{_UNITS[progress.component]}"

    def _summary_message(self, progress: EvidenceSyncProgress) -> ProgressMessage:
        details = _counts(progress, _UNITS[progress.component]) + _coverage(progress)
        if progress.component == "complete":
            if _terminal_result(progress)[1] == "失败":
                details = (f"本轮正式提交回执已确认 {len(self._committed_codes)} 股 | 整库正式覆盖未重新核对"
                           " | 公告及正文待补数量未核对")
            details += self._summary()
            details += "".join(" | " + part for part in self._feedback.summary())
            documents = self._latest.get("risk_documents")
            if documents is not None:
                unresolved = progress.workload.unresolved_failures if progress.workload is not None else None
                details += f" | 正文入库 {documents.completed} 篇 | 本轮失败 {documents.failed_documents} 篇 | 未解决失败 {unresolved if unresolved is not None else '未核对'} 篇"
            details += f" | 冷却 {progress.deferred_documents if progress.deferred_documents is not None else '未核对'} 篇 | OCR分类 {progress.ocr_documents if progress.ocr_documents is not None else '未核对'} 篇"
            details += f" | 本轮成功页 {progress.fetched_pages} | 原始资料变更 {progress.changed_rows} 条（非每股发布行） | 缓存复用 {progress.reused_partitions} 次"
            details += (f" | 原因 {_failure_text(progress.failure)}" if progress.failure else "") + _recovery(progress)
        result, state = _terminal_result(progress)
        if progress.component == "complete":
            details = "结果 " + result + " | " + details
        return ProgressMessage(tuple(ProgressField(part) for part in details.split(" | ")), task=ProgressTask("company", progress.component + "_summary", "本地库存统计" if progress.component == "resume" else "本地结果统计"),
                               state=state, finished=True, immediate=True, compact=True, heartbeat=False)

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


def _terminal_result(progress: EvidenceSyncProgress) -> tuple[str, Literal["完成", "等待", "失败", "取消"]]:
    if progress.component != "complete":
        return "阶段完成", "完成"
    failures = progress.failure.split(",")
    if "task:cancelled" in failures:
        return "取消，待处理数量未核对", "取消"
    if any(item.startswith("task:") for item in failures):
        return "失败，已确认资料保留", "失败"
    if progress.status == "deferred" or progress.pending or progress.pending_documents:
        return ("已发布但待补" if progress.completed else "待续传"), "等待"
    if progress.pending is None or progress.pending_documents is None:
        return "结束，待处理数量未核对", "等待"
    if progress.unextractable_documents:
        return "完成，存在不可提取或资料未知", "完成"
    return "完成", "完成"


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


__all__ = ["CompanyEvidenceProgress", "ReportCompanyStock", "ReportCompanyFailure", "ReportStPreparation"]
