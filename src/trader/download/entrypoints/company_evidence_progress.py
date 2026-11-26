"""Small stderr-only incremental download progress adapter."""

from __future__ import annotations

from trader.infra.progress_format import ProgressDuration, ShortProgressText
from trader.infra.serialization.canonical_codec import CanonicalDigest

import logging
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from math import ceil
from threading import Lock
from typing import Literal, TextIO, TypeVar

from trader.download.application.company_failures import CompanyFailureCode

from trader.download.application.progress_message import (
    ProgressField,
    ProgressLevel,
    ProgressMessage,
    ProgressRow,
    ProgressState,
    ProgressTask,
)
from trader.download.application.company_round_progress import (
    CompanyFieldOutcomeCounts, CompanyFormalCoverage, CompanyRequestProgress,
)
from trader.download.infra.progress_projection import ProgressNumber
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.download.domain.company_evidence import (
    COMPANY_DOCUMENT_OCR_REQUIRED,
    CompanyInputValidationFailure, CompanyLatestFinancialQuery, CompanyOperationKind, CompanyOperationProgress, CompanyPublicationProgress,
    CompanySourceFailure, CompanyStockProgress, DisclosurePartition, EvidenceSyncProgress, StructuredCompanyQuery,
)
from trader.download.domain.company_annual_income import CompanyAnnualIncomeRequest
from trader.download.domain.history_sync import HistoryStCheckFailure, HistoryStCheckProgress
from trader.download.domain.company_events import CompanyDataState
from trader.download.entrypoints.company_coverage_progress import (
    CompanyCoverageRate, CompanyFinancialBatchRows, CompanyFinancialBatchTotals, CompanyFormalCoverageRows,
    CompanyFormalCoverageSummary, CompanyProgressQuantity,
)
from trader.download.domain.company_source_records import CompanyFinancialBatchCoverage
from trader.download.domain.company_sync import (
    CompanyAnnualIncomeDownloadProgress, CompanyDisclosureDownloadProgress, CompanyDocumentDownloadProgress,
    CompanyDocumentDownloadTarget, CompanyDownloadIssue, CompanyDownloadOperation, CompanyPledgeSnapshotProgress,
    CompanySourceCancelled, CompanySourceDownloadProgress, CompanyStructuredDownloadProgress, CompanySyncFamily,
    CompanySyncFamilyResult, CompanySyncInterrupted, CompanySyncRoundResult, CompanySyncRoundScope,
    CompanySyncSourceBudgetExceeded, CompanySyncUnresolvedDirectoryRequest,
)
from trader.download.domain.download_failure import DownloadFailureObservation, DownloadValidationFailure
from trader.download.entrypoints.company_result_feedback import (
    BuildCompanyRoundFinishMessage, CompanyResultFeedback, ExplainCompanyRoundReason, ProjectCompanyRoundFamilyStatus,
)


def ReportStPreparation(
    progress: Callable[[EvidenceSyncProgress], None] | None,
    observation: HistoryStCheckProgress,
) -> None:
    if progress is not None:
        progress(
            EvidenceSyncProgress(
                "st_eligibility",
                observation.processed,
                observation.total - observation.processed,
                0,
                0,
                0,
                status="complete" if observation.summary is not None else "running",
                sources=("sina_company_name_history",),
                st_check=observation,
            )
        )


def _st_failure_message(failure: HistoryStCheckFailure) -> ProgressMessage:
    return ProgressMessage(tuple(ProgressField(value, ProgressLevel.WARNING) for value in (
        "来源 新浪", f"股票 {failure.code} {ShortProgressText(failure.name, 20)}",
        f"原因码 {failure.reason}", f"诊断 {failure.diagnostic}",
        f"尝试 {failure.attempts}/{failure.maximum_attempts}",
        f"核验累计 {failure.elapsed_seconds:.3f}秒", f"单次超时上限 {failure.timeout_seconds:.3f}秒",
        "结果 待核unknown，继续下载", "后续 不阻断本轮同步，下次ST核验重查",
    )), task=ProgressTask("参考", "ST核验失败", "ST核验失败"), state="失败", finished=True,
       stock_failure_code=failure.code, immediate=True, heartbeat=False)


def _st_progress_message(observation: HistoryStCheckProgress) -> ProgressMessage:
    summary = observation.summary
    empty_histories = tuple(failure for failure in observation.failures if failure.reason == "history_st_name_history_empty")
    warning = bool(empty_histories) or summary is not None and summary.unknown > 0
    source = "本地缓存（原来源 新浪）" if observation.cache_only else "新浪"
    fields = [f"已核验 {observation.processed}/{observation.total}股", f"已保存 {observation.persisted}/{observation.total}股"]
    if summary is not None:
        fields.extend((f"已核实非ST {summary.clear}股", f"当前或曾ST排除 {summary.ever_st}股", f"待核继续下载 {summary.unknown}股",
                       "结果 核验结束，待核股票继续下载" if warning else "结果 核验结束"))
    else:
        fields.append("结果 核验中")
    fields.append(f"来源 {source}")
    if empty_histories:
        first = empty_histories[0]
        others = f"；另{len(empty_histories) - 1}股待核" if len(empty_histories) > 1 else ""
        fields.append(
            f"待核：{first.code} {ShortProgressText(first.name, 20)}，曾用名历史为空{others}；unknown继续下载，下次ST核验重查"
        )
    return ProgressMessage(tuple(ProgressField(value, ProgressLevel.WARNING if warning else ProgressLevel.NORMAL) for value in fields),
                           task=ProgressTask("参考", "ST资格", "ST资格"),
                           state="完成" if summary is not None else "处理中", finished=summary is not None,
                           immediate=True)


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
    "announcements": "目录", "risk_documents": "正文", "facts": "核验",
    "publish": "正式发布", "finish": "收尾", "complete": "结果汇总",
}
_UNITS = {
    "prepare": "项", "resume": "篇", "st_eligibility": "股", "calendar": "份", "financial": "页",
    "annual_income": "组", "pledge": "页", "unlock": "页", "announcements": "范围",
    "risk_documents": "篇", "facts": "股", "publish": "股", "finish": "项", "complete": "股",
}
_OPERATIONS: dict[CompanyOperationKind, str] = {
    "prepare": "准备", "inventory": "核对库存", "directory": "目录查询", "download": "下载",
    "text": "文本提取", "ocr": "OCR", "document_commit": "正文入库", "structured": "结构化查询",
    "annual_income": "年度营收", "publication": "核验发布",
    "collection_commit": "资料入库", "finish": "收尾",
}
_SOURCES = {"cninfo": "巨潮", "eastmoney": "东财", "eastmoney_news": "东财", "eastmoney_annual_income": "东财", "local": "本地"}
_TASK_GROUPS = (
    ("prepare", "resume"), ("financial",), ("pledge",), ("unlock",), ("announcements",),
    ("risk_documents",), ("facts", "publish"), ("annual_income",), ("finish",),
)
_GROUP_LABELS = ("准备恢复", "财务", "质押", "解禁", "目录", "正文", "核验发布", "营收", "收尾")


def _source_label(sources: tuple[str, ...]) -> str:
    return "/".join(dict.fromkeys(_SOURCES.get(source, source) for source in sources))


_COMPANY_ROUND_TASK = ProgressTask("company", "prepare", "公司轮次")
_COMPANY_COVERAGE_TASK = ProgressTask("company", "coverage", "正式库")
_COMPANY_REQUEST_TASK = ProgressTask("company", "requests", "请求账本")
_COMPANY_SOURCE_TASK = ProgressTask("company", "sources", "来源库")
_FAMILY_NAMES = {
    CompanySyncFamily.FINANCIAL: "东财财务",
    CompanySyncFamily.PLEDGE: "东财质押",
    CompanySyncFamily.UNLOCK: "东财解禁",
    CompanySyncFamily.ANNOUNCEMENTS: "巨潮公告",
    CompanySyncFamily.ANNUAL_INCOME: "东财年度营收",
    CompanySyncFamily.FIELDS: "本地字段核验",
}
_JournalStep = tuple[str, CompanySyncFamily, int]
_Snapshot = TypeVar("_Snapshot")
_logger = logging.getLogger(__name__)
@dataclass(slots=True)
class _CompanyFamilyRun:
    seconds: float | None = None
    acknowledged: int = 0
    latest: CompanySyncFamilyResult | None = None
    operation: CompanyDownloadOperation | None = None
    operation_at: float | None = None
    step: _JournalStep | None = None


class CompanyRoundProgress:
    """Display the current invocation separately from the persisted round."""

    def __init__(self, report: SyncProgress, monotonic: Callable[[], float]) -> None:
        self._report = report
        self._monotonic = monotonic
        self._lock = Lock()
        self._families = {family: _CompanyFamilyRun() for family in CompanySyncFamily}
        self._running: tuple[CompanySyncFamily, float] | None = None
        self._last_message: ProgressMessage | None = None
        self._last_feedback = float("-inf")
        self._started_at: float | None = None
        self._finished = False
        self._requests: CompanyRequestProgress | None = None
        self._starting_requests = 0
        self._pages: dict[CompanySyncFamily, CompanyStructuredDownloadProgress] = {}
        self._latest_directory: CompanyDisclosureDownloadProgress | None = None
        self._accepted_directory_pages = 0
        self._last_material_at: float | None = None
        self._documents_downloaded = 0
        self._documents_reused = 0
        self._documents_processed = 0
        self._last_issue: tuple[CompanyDownloadIssue, _JournalStep | None] | None = None
        self._financial_batch: tuple[CompanyFinancialBatchCoverage, float] | None = None
        self._last_coverage: tuple[CompanyFieldOutcomeCounts, float] | None = None
        self._scope: CompanySyncRoundScope | None = None
        self._initial_fields: CompanyFieldOutcomeCounts | None = None
        self._next_coverage_check = 0.0
        self._next_financial_check = 0.0
        self._stopped: Callable[[], bool] | None = None
        self._read_fields: Callable[[CompanySyncRoundScope], CompanyFieldOutcomeCounts | None] | None = None
        self._read_financial: Callable[[CompanySyncRoundScope], CompanyFinancialBatchCoverage] | None = None
        self._read_ratio: Callable[[str, Literal["pledge", "unlock"]], CompanyStructuredDownloadProgress | None] | None = None
        self._read_pending: Callable[[CompanySyncRoundScope], int] | None = None
        self._pending: tuple[int, float] | None = None

    def configure_snapshots(
        self,
        *,
        stopped: Callable[[], bool],
        read_fields: Callable[[CompanySyncRoundScope], CompanyFieldOutcomeCounts | None],
        read_financial: Callable[[CompanySyncRoundScope], CompanyFinancialBatchCoverage],
        read_ratio: Callable[[str, Literal["pledge", "unlock"]], CompanyStructuredDownloadProgress | None],
        read_pending: Callable[[CompanySyncRoundScope], int],
    ) -> None:
        """Use the composition root's read-only ports on the work thread."""
        self._stopped, self._read_fields, self._read_financial, self._read_ratio = stopped, read_fields, read_financial, read_ratio
        self._read_pending = read_pending

    def begin_scope(self, scope: CompanySyncRoundScope) -> None:
        self._scope = scope
        self._initial_fields = self._snapshot(self._read_fields, scope, "正式库")
        self._next_coverage_check = self._monotonic() + 30.0
        self.report_coverage("本轮开始", self._initial_fields)
        self._sample_financial(scope)
        self._sample_pending(scope)
        if self._read_ratio is not None:
            for component in ("pledge", "unlock"):
                self._resume_ratio(scope, component)

    def _resume_ratio(self, scope: CompanySyncRoundScope, component: Literal["pledge", "unlock"]) -> None:
        reader = self._read_ratio
        if reader is None:
            return
        page = self._snapshot(lambda selected: reader(selected.identity, component), scope, "来源库")
        if page is not None:
            with self._lock:
                self._pages[page.family] = page

    def _snapshot(
        self, read: Callable[[CompanySyncRoundScope], _Snapshot | None] | None, scope: CompanySyncRoundScope, label: str,
    ) -> _Snapshot | None:
        if read is None or self._stopped is not None and self._stopped():
            return None
        try:
            return read(scope)
        except InterruptedError:
            if self._stopped is None or not self._stopped():
                raise
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            _logger.warning("公司同步%s统计不可用 category=%s", label, type(exc).__name__)
        return None

    def _sample_financial(self, scope: CompanySyncRoundScope) -> bool:
        self._next_financial_check = self._monotonic() + 30.0
        coverage = self._snapshot(self._read_financial, scope, "来源库财务")
        if coverage is None:
            return False
        self.set_financial_batch(coverage)
        if coverage.expected_pages is not None and coverage.expected_raw_rows is not None:
            page = CompanyStructuredDownloadProgress(CompanySyncFamily.FINANCIAL, coverage.accepted_pages,
                coverage.expected_pages, coverage.accepted_raw_rows, coverage.expected_raw_rows)
            with self._lock:
                self._pages[page.family] = page
        return True

    def _sample_pending(self, scope: CompanySyncRoundScope) -> str:
        count = self._snapshot(self._read_pending, scope, "来源库正文")
        if count is not None:
            self._pending = (count, self._monotonic())
            return f"已登记待分析{count}篇"
        if self._pending is None:
            return "已登记待分析篇数未核对"
        count, at = self._pending
        return f"已登记待分析{count}篇（{ProgressDuration(self._monotonic() - at)}前核对）"

    def set_financial_batch(self, coverage: CompanyFinancialBatchCoverage) -> None:
        """Attach only the caller's read-only assessment of the exact frozen batch."""
        with self._lock:
            self._financial_batch = (coverage, self._monotonic())

    def observe_requests(self, latest: CompanyRequestProgress) -> None:
        with self._lock:
            if self._requests is None or latest.round_id != self._requests.round_id:
                self._starting_requests = latest.used
            self._requests = latest

    def download(self, event: CompanySourceDownloadProgress, step: _JournalStep | None) -> None:
        """Observe source operations and committed material without conflating the ledger."""
        if isinstance(event, CompanyDownloadOperation):
            with self._lock:
                timing = self._families[event.family]
                timing.operation, timing.step = event, step
                timing.operation_at = self._monotonic()
            return
        if isinstance(event, CompanyDownloadIssue):
            self._report_download_issue(event, step)
            return
        with self._lock:
            if self._finished:
                return
            if isinstance(event, CompanyStructuredDownloadProgress):
                self._pages[event.family] = event
            elif isinstance(event, CompanyDisclosureDownloadProgress):
                self._latest_directory = event
                if event.state == "accepted":
                    self._accepted_directory_pages += 1
            elif isinstance(event, CompanyDocumentDownloadProgress):
                if event.state == "downloaded":
                    self._documents_downloaded += 1
                else:
                    self._documents_processed += 1
                    self._documents_reused += int(event.reused)
            if not isinstance(event, CompanyDisclosureDownloadProgress) or event.state != "split":
                self._last_material_at = self._monotonic()
        if isinstance(event, CompanyStructuredDownloadProgress):
            if event.accepted_pages not in {1, event.expected_pages}:
                return
        elif isinstance(event, CompanyDisclosureDownloadProgress) and event.state == "accepted":
            if event.partition.page != 1 and event.accepted_rows != event.expected_rows:
                return
        elif isinstance(event, CompanyDisclosureDownloadProgress) and event.state == "split":
            return  # The specific split reason is reported by CompanyDownloadIssue.
        elif isinstance(event, CompanyDocumentDownloadProgress):
            return  # The summary and heartbeat already report the document counters.
        self._report(ProgressMessage(
            _download_fields(event), task=_COMPANY_SOURCE_TASK, immediate=True, heartbeat=False, compact=True,
        ))

    def _report_download_issue(self, issue: CompanyDownloadIssue, step: _JournalStep | None) -> None:
        with self._lock:
            if self._finished or self._last_issue == (issue, step):
                return
            self._last_issue = (issue, step)
        if issue.code == COMPANY_DOCUMENT_OCR_REQUIRED and issue.action == "retry":
            self._report(ProgressMessage(
                (*_step_fields(step), ProgressField(_operation_text(issue.operation)),
                 ProgressField(f"原件需要文字识别，转入 OCR [{issue.code}]")),
                task=_COMPANY_SOURCE_TASK, immediate=True, heartbeat=False, compact=True,
            ))
            return
        next_step = {
            "split": "已拆分范围，后续按新范围继续",
            "retry": "保留回执，按原重试条件核对",
            "defer": "已登记暂缓，满足原条件后补取",
            "stop": "保留断点，检查原因后续传",
        }[issue.action]
        explanation = ExplainCompanyRoundReason(issue.code)
        outcome = "来源材料已接纳" if issue.material_accepted else "本次材料未接纳"
        retry_at = (ProgressField(f"可重试时间 {issue.retry_at.isoformat(timespec='seconds')}"),) if issue.retry_at else ()
        capacity = issue.capacity
        capacity_fields: tuple[ProgressField, ...] = ()
        if capacity is not None:
            limit = {"total_capacity": "原件总容量", "download_headroom": "下载预留", "physical_space": "磁盘余量"}[capacity.limit]
            capacity_fields = (ProgressField(
                f"{'分析' if capacity.analysis else '下载'}预约受限 [{capacity.limit}] {limit}，单位为字节 | "
                f"本次申请 {capacity.requested_bytes}，原件占用 {capacity.used_bytes}/{capacity.capacity_bytes}，"
                f"已预约 {capacity.held_bytes}（含分析 {capacity.analysis_held_bytes}），分析预留 {capacity.analysis_reserve_bytes}，"
                f"磁盘可用 {capacity.free_bytes}，最低保留 {capacity.free_reserve_bytes}"
            ),)
        self._report(ProgressMessage(
            (
                *_step_fields(step),
                ProgressField(_operation_text(issue.operation)),
                ProgressField(f"{explanation or '来源作业异常'} [{issue.code}]", ProgressLevel.WARNING),
                *capacity_fields,
                ProgressField(outcome),
                ProgressField(next_step),
                *retry_at,
            ), task=_COMPANY_SOURCE_TASK, immediate=True, heartbeat=False, compact=True,
        ))

    def observe_family(self, family: CompanySyncFamilyResult) -> None:
        self.advance(family)
        scope = self._scope
        now = self._monotonic()
        if scope is None:
            return
        if family.family is CompanySyncFamily.FINANCIAL and (
            now >= self._next_financial_check or family.result.processed_scope_complete
        ):
            self._sample_financial(scope)
        if family.family is CompanySyncFamily.FIELDS and family.result.changed_codes and now >= self._next_coverage_check:
            self._next_coverage_check = now + 30.0
            current = self._snapshot(self._read_fields, scope, "正式库")
            if current is not None:
                self.report_coverage("同步中", current)

    def observe_financial_scoring(self, scope: CompanySyncRoundScope) -> None:
        if scope == self._scope and (current := self._snapshot(self._read_fields, scope, "正式库")) is not None:
            self.report_coverage("同步中", current)

    def start(self, requested: int) -> None:
        message = ProgressMessage(
            (
                ProgressField(f"本次请求总体 {requested}股"),
                ProgressField("核对固定轮次、请求账本和资料完整性，恢复本地断点。"),
            ),
            task=_COMPANY_ROUND_TASK,
            state="开始",
        )
        with self._lock:
            self._started_at = self._monotonic()
            self._last_message = message
            self._last_feedback = self._started_at
        self._report.register_observer("company", self._heartbeat, task_key="prepare")
        self._report(message)

    def report_coverage(
        self,
        phase: Literal["本轮开始", "同步中", "本轮收尾"],
        counts: CompanyFieldOutcomeCounts | None,
        previous: CompanyFormalCoverage | None = None,
    ) -> None:
        """Emit a read-only snapshot supplied by the orchestration boundary."""
        coverage = counts.coverage if counts is not None else None
        fields: tuple[ProgressField, ...]
        rows: tuple[ProgressRow, ...]
        fields = (ProgressField(phase), ProgressField("来源 本地正式公司库"))
        if phase == "本轮收尾" and coverage is None and self._last_coverage is not None:
            counts, verified_at = self._last_coverage
            coverage = counts.coverage
            age = ProgressDuration(self._monotonic() - verified_at)
            fields += (ProgressField(f"收尾未重新核对，以下为{age}前已核对的正式快照", ProgressLevel.WARNING),)
        elif counts is not None and coverage is not None:
            last_snapshot = self._last_coverage
            self._last_coverage = (counts, self._monotonic())
            if phase == "同步中" and last_snapshot is not None and last_snapshot[0].publication_hash == counts.publication_hash:
                return
        if self._scope is not None:
            fields += (ProgressField(f"本轮资料截止 {self._scope.cutoff.isoformat(timespec='seconds')}"),)
        if counts is not None:
            fields += (ProgressField(f"发布 {counts.publication_hash[:12]}"),)
        if coverage is None:
            fields += (ProgressField("正式字段缺失比例未核对", ProgressLevel.WARNING),)
            rows = ()
        else:
            fields += (
                ProgressField(f"固定总体{coverage.stocks}股"),
                ProgressField(f"完整财务与风险覆盖缺口{CompanyCoverageRate(coverage.recommendation_missing, coverage.recommendation_expected)}"
                    "（每股8项财务和3项风险覆盖）"),
            )
            if coverage.financial_scoring_stocks is not None:
                fields += (ProgressField(f"D25已发布评分观测{CompanyCoverageRate(coverage.financial_scoring_stocks, coverage.stocks)}股"),)
            if phase == "本轮收尾":
                fields += CompanyFormalCoverageSummary(coverage, previous)
            rows = tuple(replace(row, label=f"正式库·{row.label}") for row in CompanyFormalCoverageRows(coverage))
        self._report(ProgressMessage(fields, task=_COMPANY_COVERAGE_TASK, finished=phase == "本轮收尾",
            immediate=True, heartbeat=False, compact=True, rows=rows, stage_width=52))

    @contextmanager
    def activity(self, family: CompanySyncFamily) -> Iterator[None]:
        started_at = self._monotonic()
        with self._lock:
            if self._running is not None or self._finished:
                raise ValueError("company_sync_progress_activity_invalid")
            self._running = (family, started_at)
            timing = self._families[family]
            timing.operation, timing.operation_at, timing.step = None, None, None
        try:
            yield
        except (
            CompanySyncInterrupted, CompanySourceCancelled, CompanyInputValidationFailure, CompanySourceFailure,
            DownloadValidationFailure, CompanySyncSourceBudgetExceeded, CompanySyncUnresolvedDirectoryRequest,
        ):
            raise
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            if timing.operation is not None:
                self._report(ProgressMessage(
                    (
                        *_step_fields(timing.step),
                        ProgressField(_operation_text(timing.operation)),
                        ProgressField(f"本次步骤异常 [{CompanyFailureCode(exc)}]，提交状态见续传核对", ProgressLevel.ERROR),
                    ),
                    task=_COMPANY_SOURCE_TASK, state="失败", immediate=True, heartbeat=False,
                ))
            raise
        finally:
            with self._lock:
                timing = self._families[family]
                timing.seconds = (timing.seconds or 0.0) + self._monotonic() - started_at
                self._running = None

    def advance(self, family: CompanySyncFamilyResult) -> None:
        failure = family.result.failure
        state: ProgressState = "阻塞" if failure == "source_attempt_unconfirmed" else "失败" if failure else "处理中"
        with self._lock:
            if self._finished:
                return
            timing = self._families[family.family]
            previous_gaps = timing.latest.result.gaps if timing.latest is not None else ()
            previous_failure = timing.latest.result.failure if timing.latest is not None else None
            show_issues = failure != previous_failure or frozenset(family.result.gaps) != frozenset(previous_gaps)
            timing.acknowledged += 1
            timing.latest = family
            operation, step = timing.operation, timing.step
            timing.operation, timing.operation_at, timing.step = None, None, None
            now = self._monotonic()
            message = ProgressMessage(
                (
                    ProgressField("最近确认调度步"),
                    ProgressField(f"{_FAMILY_NAMES[family.family]} 第{family.steps}步"),
                    *_family_progress_fields(family),
                ),
                task=_COMPANY_ROUND_TASK,
                state="处理中",
            )
            self._last_message = message
            if not show_issues and now - self._last_feedback < 5.0:
                return
            self._last_feedback = now
        if show_issues:
            location = (ProgressField(_operation_text(operation)),) if failure and operation is not None else ()
            action = (ProgressField("来源输入未通过接纳校验，保留原回执"),) if failure and (
                "supplier_input_rejected" in family.result.gaps) else ()
            message = replace(
                message, fields=(*_step_fields(step), *message.fields, *location, *_family_issue_fields(family), *action),
                state=state, immediate=True,
            )
        else:
            with self._lock:
                message = replace(message, fields=(*message.fields, *self._heartbeat_fields(now)))
        self._report(message)

    def finish(
        self,
        result: CompanySyncRoundResult,
        *,
        fields: CompanyFieldOutcomeCounts | None = None,
        previous: CompanyFieldOutcomeCounts | None = None,
        time_limit_seconds: float | None = None,
        step_limit: int | None = None,
    ) -> None:
        stopped_at = self._monotonic()
        financial_fresh = False
        pending = ""
        if self._scope == result.scope:
            fields = self._snapshot(self._read_fields, result.scope, "正式库") if result.publication_hash else None
            previous = self._initial_fields
            financial_fresh = self._sample_financial(result.scope)
            pending = self._sample_pending(result.scope)
        with self._lock:
            if self._running is not None:
                raise ValueError("company_sync_progress_activity_unfinished")
            self._finished = True
            started_at = self._started_at
            confirmed_steps = sum(item.acknowledged for item in self._families.values())
            financial_batch = self._financial_batch
            requests = self._request_fields()
            ledger = self._requests if self._requests is not None and self._requests.round_id == result.round_id else None
            source = self._source_fields(stopped_at)
            directory = self._latest_directory
            source_rows = tuple(
                ProgressRow(f"来源库·{_FAMILY_NAMES[page.family]}", _download_fields(page))
                for page in self._pages.values()
            )
        self._report.finish_owner("company")
        request_current = tuple(family.family for family in result.families) == result.scope.families and all(
            family.source_calls is not None and family.unknown_attempts is not None for family in result.families
        )
        if result.paused or started_at is not None:
            self._report(BuildCompanyRoundFinishMessage(
                result,
                _COMPANY_ROUND_TASK,
                elapsed_seconds=stopped_at - started_at if started_at is not None else None,
                time_limit_seconds=time_limit_seconds,
                confirmed_steps=confirmed_steps,
                step_limit=step_limit,
                ledger=ledger,
                request_current=request_current,
            ))
        published = fields if fields is not None and fields.publication_hash == result.publication_hash else None
        coverage = published.coverage if published is not None else None
        former = previous.coverage if previous is not None else None
        if started_at is not None:
            if not request_current:
                requests = (ProgressField("终态累计待核，以下为最近账本确认", ProgressLevel.WARNING), *requests)
            self._report(ProgressMessage(
                requests, task=_COMPANY_REQUEST_TASK, finished=True, heartbeat=False, compact=True,
            ))
            self._report(ProgressMessage(
                (ProgressField("本轮收尾"), *source, ProgressField(pending or "已登记待分析篇数未核对"),
                    *(_download_fields(directory) if directory is not None else ())),
                task=_COMPANY_SOURCE_TASK, finished=True, heartbeat=False, compact=True, rows=source_rows, stage_width=52,
            ))
        if financial_batch is not None:
            batch, verified_at = financial_batch
            details = CompanyFinancialBatchTotals(batch)
            if self._scope is not None and not financial_fresh:
                age = ProgressDuration(self._monotonic() - verified_at)
                details = (ProgressField(f"收尾未重新核对，以下为{age}前的来源统计", ProgressLevel.WARNING), *details)
            self._report(ProgressMessage(
                details,
                task=ProgressTask("company", "financial_values", "来源库·东财财报字段"),
                rows=CompanyFinancialBatchRows(batch), finished=True, heartbeat=False, compact=True, stage_width=52,
            ))
        elif started_at is not None:
            self._report(ProgressMessage(
                (ProgressField("财报字段统计未核对", ProgressLevel.WARNING),),
                task=_COMPANY_SOURCE_TASK, finished=True, heartbeat=False, compact=True,
            ))
        for family in result.families:
            timing = self._families[family.family]
            details = (*_family_progress_fields(family), *_family_issue_fields(family), *_invocation_fields(timing, family.family))
            database = "正式库" if family.family is CompanySyncFamily.FIELDS else "来源库"
            if family.family is CompanySyncFamily.FIELDS:
                details += _published_field_totals(published)
                if published is not None and previous is not None:
                    details += (ProgressField(f"本次正式已核字段净增{published.verified - previous.verified:+d}项"),)
            self._report(ProgressMessage(
                details,
                task=ProgressTask("company", family.family.value, f"{database}·{_FAMILY_NAMES[family.family]}"),
                state=ProjectCompanyRoundFamilyStatus(family, result),
                finished=True,
                stage_elapsed_seconds=timing.seconds if timing.seconds is not None else "not_run",
            ))
        if started_at is not None or coverage is not None or self._last_coverage is not None:
            self.report_coverage("本轮收尾", published, former if coverage is not None else None)

    def _request_fields(self) -> tuple[ProgressField, ...]:
        latest = self._requests
        if latest is None:
            return (ProgressField("请求累计未核对"),)
        return (
            ProgressField(f"轮次{latest.round_id[:12]}"),
            ProgressField(f"本次新增{latest.used - self._starting_requests}次"),
            ProgressField(f"轮次累计{latest.used}次（不限次数）" if latest.limit is None
                          else f"轮次累计{latest.used}/{latest.limit}次"),
            ProgressField(f"未确认{latest.unknown}次"),
        )

    def _source_fields(self, now: float) -> tuple[ProgressField, ...]:
        pages = []
        for family in (CompanySyncFamily.FINANCIAL, CompanySyncFamily.PLEDGE, CompanySyncFamily.UNLOCK):
            if family is CompanySyncFamily.FINANCIAL and self._financial_batch and self._financial_batch[0].market_prefix:
                batch = self._financial_batch[0]
                state = "逐股最新查询已闭合" if batch.pages_complete else "逐股最新查询中"
                value = f"{state}，已入库{batch.accepted_pages}页，已有报告{batch.latest_observed_stocks}/{batch.population}股"
            elif (page := self._pages.get(family)) is not None:
                value = f"{CompanyCoverageRate(page.accepted_pages, page.expected_pages)}页"
            else:
                value = "总页数待确认"
            pages.append(ProgressField(f"{_FAMILY_NAMES[family]} {value}"))
        elapsed = now - self._started_at if self._started_at is not None else 0.0
        throughput = (
            (ProgressField(f"本次目录新入库{self._accepted_directory_pages}页，平均"
                           f"{60 * self._accepted_directory_pages / elapsed:.2f}页/分"),)
            if elapsed > 0 and self._accepted_directory_pages else ()
        )
        return (
            *pages,
            *throughput,
            ProgressField(f"本次公告正文 下载{self._documents_downloaded}篇／复用{self._documents_reused}篇／"
                f"入库{self._documents_processed}篇"),
        )

    def _heartbeat_fields(self, now: float) -> tuple[ProgressField, ...]:
        material_age = (
            f"距最近入库 {ProgressDuration(now - self._last_material_at)}"
            if self._last_material_at is not None else "本次尚无来源入库回执"
        )
        return ProgressField(material_age), *self._source_fields(now), *self._request_fields()

    def _heartbeat(self) -> ProgressMessage | None:
        with self._lock:
            now = self._monotonic()
            message = self._last_message
            if self._finished or message is None or now - self._last_feedback < 5.0:
                return None
            self._last_feedback = now
            details = self._heartbeat_fields(now)
            if self._running is None:
                return replace(message, fields=(*message.fields, *details), state="等待")
            family, started_at = self._running
            timing = self._families[family]
            if timing.operation_at is not None:
                started_at = timing.operation_at
            operation = _operation_text(timing.operation) if timing.operation is not None else _FAMILY_NAMES[family]
            current = ProgressField(f"当前执行 {operation} 已用 {ProgressDuration(now - started_at)}")
            return replace(message, fields=(current, *details, *message.fields), state="处理中", immediate=False)


def _invocation_fields(timing: _CompanyFamilyRun, family: CompanySyncFamily) -> tuple[ProgressField, ...]:
    if timing.seconds is None:
        description = (
            f"本次确认{timing.acknowledged}步，未派发族内作业"
            if timing.acknowledged else "本次未执行，沿用账本"
        )
        return (ProgressField(description),)
    duration = ProgressField(f"本次确认{timing.acknowledged}步，执行用时{timing.seconds:.2f}秒")
    if timing.latest is not None and family is CompanySyncFamily.FIELDS:
        return duration, ProgressField(f"本次最近确认核验{CompanyProgressQuantity(timing.latest.result.accepted_inputs)}股")
    return (duration,)


def _published_field_totals(fields: CompanyFieldOutcomeCounts | None) -> tuple[ProgressField, ...]:
    if fields is None:
        return (ProgressField("正式字段及待处理正文统计未核对", ProgressLevel.WARNING),)
    return (
        ProgressField(
            f"正式字段核实{fields.verified}项，有值待核{fields.present_unverified}项，"
            f"其他待核{fields.other_pending}项，已证未披露{fields.not_disclosed}项，不适用{fields.not_applicable}项"
        ),
        ProgressField(f"各股待处理正文合计{fields.pending_documents}篇次（正式库全发布）"),
    )


def _step_fields(step: _JournalStep | None) -> tuple[ProgressField, ...]:
    return () if step is None else (ProgressField(f"轮次{step[0][:12]} · {_FAMILY_NAMES[step[1]]}第{step[2]}步"),)


def _directory_scope(partition: DisclosurePartition) -> str:
    scope = f"巨潮·{_market_label(partition.market)} {partition.start}至{partition.end}"
    query = partition.query
    if query is None:
        return scope
    board = ShortProgressText(query.plate, 16) or "全板"
    codes = "、".join(item.code for item in query.securities[:3])
    suffix = f" 股票{codes}" if codes else ""
    return f"{scope} 板块{board}{suffix} 查询{CanonicalDigest(query)[:10]}"


def _operation_text(operation: CompanyDownloadOperation) -> str:
    target, kind = operation.target, operation.kind
    if kind == "pledge_snapshot":
        return "东财·确认质押快照日期"
    if isinstance(target, CompanyLatestFinancialQuery):
        group = f"定向查询{'、'.join(target.securities)}" if target.securities else "全市场按报告期倒序"
        return f"东财财务 {group} 第{target.page}页"
    if isinstance(target, StructuredCompanyQuery):
        return f"{_FAMILY_NAMES[operation.family]} {target.start}至{target.end} 第{target.page}页"
    if isinstance(target, DisclosurePartition):
        action = "恢复目录" if kind == "announcement_recover" else "目录"
        return f"{_directory_scope(target)} {action}第{target.page}页"
    if isinstance(target, CompanyDocumentDownloadTarget):
        action = "下载正文" if kind == "document_download" else f"提取正文（{_DOCUMENT_ROUTES[operation.route]}）"
        return f"{_source_label((target.source,))}·{action} 股票{target.code} 公告{ShortProgressText(target.official_id, 48)}"
    if isinstance(target, CompanyAnnualIncomeRequest):
        periods = "／".join(str(day) for day in target.report_dates)
        return f"东财·年度营收 股票{target.code} 报告期{periods}"
    return _FAMILY_NAMES[operation.family]


_DOCUMENT_ROUTES = {None: "本地", "standard": "标准", "ocr": "OCR", "limits": "扩展"}


def _raw_rows(rows: int | None, total: int | None) -> str:
    if total == 0:
        return "原始行0条，来源返回空集"
    return f"原始行{CompanyProgressQuantity(rows)}/{CompanyProgressQuantity(total)}条"


def _download_fields(
    event: CompanyStructuredDownloadProgress | CompanyDisclosureDownloadProgress
    | CompanyPledgeSnapshotProgress | CompanyAnnualIncomeDownloadProgress,
) -> tuple[ProgressField, ...]:
    if isinstance(event, CompanyStructuredDownloadProgress):
        state = "采集已闭合" if event.accepted_pages == event.expected_pages and event.accepted_rows == event.expected_rows else "采集中"
        return (
            ProgressField(f"{_FAMILY_NAMES[event.family]} {state}"),
            ProgressField(f"已入库{CompanyCoverageRate(event.accepted_pages, event.expected_pages)}页"),
            ProgressField(_raw_rows(event.accepted_rows, event.expected_rows)),
        )
    if isinstance(event, CompanyPledgeSnapshotProgress):
        return (ProgressField(f"东财质押 快照日期已确认 {event.day}"),)
    if isinstance(event, CompanyAnnualIncomeDownloadProgress):
        return (ProgressField(f"东财年度营收 股票{event.request.code} 已接纳{event.accepted_rows}条"),)
    location = _directory_scope(event.partition)
    if event.state == "split":
        return (ProgressField(f"最近目录动作 {location} 已拆分"),)
    if event.state == "reconciled":
        return (ProgressField(f"{location} 子查询覆盖已核对"), ProgressField(_raw_rows(event.accepted_rows, event.expected_rows)))
    state = "已恢复" if event.state == "recovered" else "已入库"
    total = max(1, ceil(event.expected_rows / event.partition.page_size)) if event.expected_rows is not None else None
    pages = f"{CompanyCoverageRate(event.partition.page, total)}页" if total is not None else f"{event.partition.page}页，总页数待确认"
    return (ProgressField(f"{location} {state} {pages}"), ProgressField(_raw_rows(event.accepted_rows, event.expected_rows)))


def _family_progress_fields(family: CompanySyncFamilyResult) -> tuple[ProgressField, ...]:
    accepted = (
        f"累计核验{CompanyProgressQuantity(family.accepted_inputs)}股次/{family.steps}步，累计变动{len(family.result.changed_codes)}股"
        if family.family is CompanySyncFamily.FIELDS
        else f"累计接纳输入{CompanyProgressQuantity(family.accepted_inputs)}项"
    )
    progress = ProgressField(
        f"{family.family.value} {accepted}，"
        f"本族请求预算已占用{CompanyProgressQuantity(family.source_calls)}，未知{CompanyProgressQuantity(family.unknown_attempts)}")
    if family.result.retry_after_seconds is not None:
        return progress, ProgressField(f"冷却{family.result.retry_after_seconds:g}秒后自动复核，可随时取消")
    if family.family is CompanySyncFamily.ANNUAL_INCOME and family.result.business_complete and family.accepted_inputs == 0:
        return progress, ProgressField("本轮无待处理的合同营收任务。")
    return (progress,)


def _family_issue_fields(family: CompanySyncFamilyResult) -> tuple[ProgressField, ...]:
    fields = (
        ProgressField(f"缺口{len(family.result.gaps)}项"),
        *((ProgressField(f"错误 {family.result.failure}", ProgressLevel.ERROR),) if family.result.failure is not None else ()),
        *((ProgressField(f"缺口原因 {','.join(family.result.gaps)}", ProgressLevel.WARNING),) if family.result.gaps else ()),
    )
    reasons = tuple(dict.fromkeys(code for code in (family.result.failure, *family.result.gaps) if code is not None))
    explanations = tuple(text for code in reasons if (text := ExplainCompanyRoundReason(code)) is not None)
    if explanations:
        fields += (ProgressField(f"说明 {'。'.join(explanations)}。"),)
    return fields


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
        if progress.component == "st_eligibility" and progress.st_check is not None:
            return (*(_st_failure_message(failure) for failure in progress.st_check.failures
                      if failure.reason != "history_st_name_history_empty"),
                    _st_progress_message(progress.st_check))
        if progress.component in {"st_eligibility", "calendar"}:
            return (self._message(progress, immediate=True),)
        if progress.component == "resume":
            self._last_emitted = self._monotonic()
            return (self._message(progress, immediate=True), self._summary_message(progress))
        self._foreground = progress.component
        new_failures = set(filter(None, progress.failure.split(","))) - self._reported_failures
        self._reported_failures.update(new_failures)
        messages: list[ProgressMessage] = []
        if (progress.component in {"financial", "pledge", "unlock", "announcements"}
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
        parts.append(f"本轮已获取{len(self._received_codes)}股、核验{len(self._stocks)}/{CompanyProgressQuantity(self._population)}股、提交{len(self._committed_codes)}股（去重）")
        parts.append(f"已知待补{len(self._waiting_codes)}股；未核验{CompanyProgressQuantity(None if self._population is None else max(0, self._population - len(self._stocks)))}股")
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
            parts.append(f"合格股票已获取{len(self._component_codes.get(progress.component, set()))}/{CompanyProgressQuantity(self._population)}股（本查询已见，非覆盖）")
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
            parts.append(f"正文冷却{CompanyProgressQuantity(documents.deferred_documents)}篇、不可提取{CompanyProgressQuantity(documents.unextractable_documents)}篇、未解决失败{documents.unresolved_document_failures}篇、已分析待发{CompanyProgressQuantity(documents.unpublished_documents)}篇")
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
            return f"正文已入库{progress.completed}篇，可处理待补{CompanyProgressQuantity(due)}篇；本轮失败{progress.failed_documents}篇"
        if progress.component == "facts":
            return f"本批核验{progress.completed}/{total}股；本轮去重核验{len(self._stocks)}股"
        if progress.publication is not None:
            return f"本批{'提交' if progress.publication.state == 'committed' else '待发'}{len(progress.publication.codes)}股；本轮去重提交{len(self._committed_codes)}股"
        if progress.component == "publish":
            return f"正式覆盖{progress.completed}/{CompanyProgressQuantity(self._population)}股；本轮提交{len(self._committed_codes)}股"
        if progress.component == "announcements":
            return f"目录已持久推进{progress.completed}个范围，已知待补{CompanyProgressQuantity(progress.pending)}个范围（可继续细分）；已知待取{progress.known_pages}页、待发现页数的查询{progress.unknown_queries}个"
        if progress.component == "complete":
            return f"目录待补{CompanyProgressQuantity(progress.pending)}个范围；正文待补{CompanyProgressQuantity(progress.pending_documents)}篇；本轮变更{len(self._changed_codes)}股（去重）"
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


__all__ = [
    "CompanyEvidenceProgress",
    "CompanyRoundProgress",
    "ReportCompanyStock",
    "ReportCompanyFailure",
    "ReportStPreparation",
]
