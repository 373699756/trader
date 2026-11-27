"""Render confirmed company outcomes and failure evidence without performing I/O."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from trader.download.application.company_round_progress import CompanyRequestProgress
from trader.download.application.progress_message import (
    ProgressField, ProgressLevel, ProgressMessage, ProgressRow, ProgressState, ProgressTask,
)
from trader.download.application.sync_data import SyncResult
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import (
    CompanyOperationResult, CompanyPublicationProgress, CompanyRecoveryDecision, CompanyStockProgress,
    DisclosurePartition, StructuredCompanyQuery,
)
from trader.download.domain.company_sync import (
    CompanySyncFamily,
    CompanySyncFamilyResult,
    CompanySyncPauseReason,
    CompanySyncRoundRebuildBlocked,
    CompanySyncRoundResult,
    CompanySyncStepResult,
)
from trader.download.domain.download_failure import DownloadFailureObservation, DownloadFailureRecord
from trader.infra.progress_format import ShortProgressText

_SOURCES = {"cninfo": "巨潮", "eastmoney": "东财", "eastmoney_news": "东财", "local": "本地"}
_LABELS = {
    "prepare": "准备资料", "resume": "恢复库存", "financial": "财务", "pledge": "质押", "unlock": "解禁",
    "announcements": "目录", "risk_documents": "正文", "annual_income": "年度营收",
    "publish": "股票发布", "finish": "收尾",
}
_COMPONENTS = {
    "download": "risk_documents", "text": "risk_documents", "ocr": "risk_documents",
    "document_commit": "risk_documents", "directory": "announcements", "publication": "publish",
    "inventory": "resume",
}
_ACTIONS = {
    "download": "正文获取", "text": "文本提取", "ocr": "OCR", "document_commit": "正文入库",
    "directory": "目录查询", "structured": "结构化查询", "collection_commit": "资料落库",
    "publication": "正式发布", "inventory": "库存核对", "prepare": "资料准备", "finish": "收尾",
    "annual_income": "年度营收",
}
_SUMMARY_FAMILIES = {"announcements": "公告", "fields": "字段核验"}
_PAUSE_EXPLANATIONS: dict[CompanySyncPauseReason, str] = {
    "deadline": "达到公司轮次时限",
    "step_limit": "达到本次调度总步数上限",
    "request_limit": "固定轮次的来源请求额度已用完",
    "source_window": "来源派发窗口已结束，剩余时间留给确认和本地处理",
}
_SUMMARY_FAILURES = {
    "preparation_ReferenceDatabaseIncompatibleError": "ST资格及交易日历参考库结构不兼容",
    "preparation_AkshareCalendarTimeoutError": "交易日历准备超时",
    "CompanyDatabaseIncompatibleError": "公司资料库或请求账本结构不兼容，具体范围见上方诊断",
    CompanySyncRoundRebuildBlocked.category: "原请求轮次仍需核对，空库重下载暂缓",
    "ProcessLockError": "另一个任务持有公司同步锁",
}
_REASONS = {
    "timeout": "请求超时", "HttpTimeout": "请求超时", "HttpConnectionError": "连接失败",
    "access_denied": "来源拒绝访问", "rate_limited": "来源限流", "source_stopped": "来源暂缓",
    "disclosure_document_ocr_timeout": "OCR超时", "disclosure_document_ocr_timeout_exhausted": "OCR自动重试已用尽",
    "disclosure_document_unreadable": "PDF无法解析", "disclosure_document_no_text": "PDF未提取到文字",
    "disclosure_document_ocr_low_quality": "OCR文字或置信度不足",
    "disclosure_document_ocr_unavailable": "本机OCR工具不可用",
    "OperationalError": "数据库操作失败", "DatabaseError": "数据库操作失败",
    "OSError": "系统I/O失败", "RuntimeError": "运行步骤失败", "ValueError": "值校验未通过",
    "JSONDecodeError": "接口响应不是有效JSON",
}
_ROUND_REASON_EXPLANATIONS = {
    "financial_first_disclosure_revision_unverified": "财报首次披露时间与后续修订链待核",
    "latest_financial_population_unverified": "各股最新财报的全量覆盖待核",
    "pledge_population_coverage_unverified": "质押快照是否覆盖全部合格股票待核",
    "pledge_total_share_denominator_unverified": "质押比例的总股本分母待核",
    "unlock_population_coverage_unverified": "未来解禁窗口是否覆盖全部合格股票待核",
    "unlock_total_share_denominator_unverified": "解禁比例的总股本分母待核",
    "source_attempt_unconfirmed": "旧请求已占用额度，响应或落库凭据待核",
    "previous_directory_attempt_unconfirmed": "该目录请求已在旧轮次占用额度，等待原响应凭据",
    "accepted_input_count_unconfirmed": "旧步骤接纳数量待核",
    "issuer_revision_relationship_unverified": "同一发行人公告的新旧修订关系待核",
    "persistent_risk_initial_state_unverified": "公告窗口开始时仍生效的历史风险待核",
    "field_proof_incomplete": "仍有适用字段缺值或字段凭据待核",
    "request_counts_unconfirmed": "步骤中断，终态请求数待核",
    "source_budget_exhausted": "本轮来源请求额度已用完",
    "company_raw_capacity_unavailable": "公告原件空间预约未通过，恢复容量后可沿原轮次续传",
    "step_interrupted": "本次步骤中断，沿原回执续传",
    "company_round_page_number_mismatch": "财务返回的页码与请求不符",
    "company_round_total_changed": "财务分页总数变化",
    "company_round_observation_time_mismatch": "财务观察时点与固定轮次不符",
    "company_financial_group_count_mismatch": "财务查询组的原始行数、页数或回执不一致",
    "company_financial_group_order_changed": "财务相邻页的排序边界发生变化",
    "company_financial_group_order_missing": "已保存的财务页缺少排序边界",
    "company_financial_prefix_receipt_missing": "逐股最新财报查询缺少市场页回执",
    "company_financial_prefix_receipt_mismatch": "逐股最新财报查询与市场页回执不符",
    "company_financial_prefix_targets_invalid": "财务补查股票与已保存的缺口不符",
    "company_financial_exact_scope_missing": "财务定向补查缺少原市场范围",
    "company_financial_exact_scope_changed": "定向财务响应含请求范围外的股票",
    "company_financial_exact_version_changed": "定向财务查询与全市场已取得的报告版本冲突",
    "company_financial_exact_witness_missing": "定向财务响应未复现用于核对的已知报告",
    "company_round_notice_outside_window": "财务披露日期超出本轮窗口",
    "cninfo_partition_changed": "巨潮目录返回总数变化",
    "cninfo_page_truncated": "巨潮目录返回页截断",
    "cninfo_page_count_inconsistent": "巨潮目录分页计数不一致",
    "cninfo_duplicate_page_links": "巨潮目录出现重复公告链接，本页无法确认覆盖完整",
    "cninfo_partition_split_page_limit": "巨潮目录查询范围超过单个分区的100页上限",
    "supplier_input_rejected": "来源返回内容未通过校验，具体原因见错误码",
    "source_retry_pending": "来源请求暂时失败，冷却到期后继续复核",
    "announcement_retry_pending": "公告资料正在冷却或等待续传，其余可处理资料已调度",
    "latest_requested_cutoff_pending": "正从上次断点续传，尚未更新到本次截止时刻",
    "latest_announcement_window_pending": "本次增量公告仍待核对",
    "latest_announcement_original_unverified": "本次增量公告有未能读取的原件，相关字段保留待核",
    "latest_financial_publication_pending": "部分最新来源报告仍待正式采用",
    "latest_current_inputs_pending": "最新来源批次或正式字段仍待核对",
    "announcement_directory_retry": "本页目录已暂缓，继续处理其他可用目录和公告正文",
    "population_members_became_ineligible": "原轮次中部分股票已不符合当前资格",
    "new_population_members_wait_for_refresh": "新增合格股票等待下一轮次",
}
_RECOVERIES = {
    "retry": "自动重试", "cooldown": "冷却续传", "unextractable": "不可提取／资料未知",
    "source_deferred": "来源暂缓", "ocr_queued": "转OCR", "recording_failed": "恢复登记失败",
}
_ERROR_COMPONENTS = {"company_directory": "announcements", "company_document": "risk_documents"}
_BusinessKey = tuple[str, str, str]
_FailureKey = tuple[str, str, str, str]


def ExplainCompanyRoundReason(code: str) -> str | None:
    """Return a fixed explanation for a recognized round reason code."""
    return _ROUND_REASON_EXPLANATIONS.get(code)


def ExplainCompanySyncPause(reason: CompanySyncPauseReason | None) -> str:
    """Describe the exact guard that prevented further work, when recorded."""
    return _PAUSE_EXPLANATIONS[reason] if reason is not None else "本轮暂停原因未核对"


def BuildCompanyRoundFinishMessage(
    result: CompanySyncRoundResult,
    task: ProgressTask,
    *,
    elapsed_seconds: float | None,
    time_limit_seconds: float | None,
    confirmed_steps: int,
    step_limit: int | None,
    ledger: CompanyRequestProgress | None,
    request_current: bool,
) -> ProgressMessage:
    """Render the observed stop reason and the distinct invocation and round limits."""
    state: ProgressState
    if result.paused:
        reason = "收到取消请求" if result.cancelled else ExplainCompanySyncPause(result.pause_reason)
        checkpoint = "已有断点保留" if result.round_id else "本轮断点未核对"
        summary = ProgressField(f"{reason}，{checkpoint}", ProgressLevel.WARNING)
        state = "取消" if result.cancelled else "暂停"
    elif result.latest is not None and result.latest.ready:
        summary, state = ProgressField("最新字段检查完成，保留各字段的来源和核验状态"), "完成"
    elif result.business_complete:
        summary, state = ProgressField("本轮处理完成"), "完成"
    elif result.processing_complete:
        summary, state = ProgressField("本次调度结束，正式资料仍待补证"), "待补证"
    else:
        summary, state = ProgressField("本次调度结束，仍有工作待继续"), "待继续"
    usage: list[ProgressField] = []
    if elapsed_seconds is not None:
        duration = "无单次时限" if time_limit_seconds is None else f"单次时限{time_limit_seconds:g}秒"
        usage.append(ProgressField(f"本次公司作业用时{elapsed_seconds:.2f}秒，{duration}"))
        steps = f"{confirmed_steps}步（不限步数）" if step_limit is None else f"{confirmed_steps}/{step_limit}步"
        usage.append(ProgressField(f"本次确认调度{steps}"))
        occupancy = "已占用" if request_current else "最近确认占用"
        if ledger is None:
            request_usage = "固定轮次请求额度未核对"
        elif ledger.limit is None:
            request_usage = f"固定轮次请求{occupancy}{ledger.used}次（不限次数）"
        else:
            request_usage = f"固定轮次请求额度{occupancy}{ledger.used}/{ledger.limit}次"
        usage.append(ProgressField(request_usage))
    return ProgressMessage((summary, *usage), task=task, state=state, finished=True)


def ProjectCompanyRoundFamilyStatus(family: CompanySyncFamilyResult, result: CompanySyncRoundResult) -> ProgressState:
    """Project a family outcome without promoting progress to business completeness."""
    step = family.result
    if step.failure == "source_attempt_unconfirmed":
        return "阻塞"
    if step.failure:
        return "失败"
    if step.business_complete:
        return "完成"
    if result.cancelled and step.can_continue:
        return "取消"
    if "source_budget_exhausted" in step.gaps or (result.budget_exhausted and step.can_continue):
        return "暂停"
    return "待继续" if step.can_continue or (step.pending_count is None and not step.gaps) else "待补证"


def FormatCompanySyncSummary(result: SyncResult, state: ProgressState) -> str:
    """Summarize an explicit company command without projecting internal JSON."""
    status: str = state
    task = next((item for item in result.tasks if item.task == "company"), None)
    company = task.company if task is not None else None
    if company is None:
        heading = f"公司同步：{status}"
        reason = result.failure or (task.reason if task is not None else None)
        if reason is None:
            return heading
        return f"{heading}\n原因：{_summary_failure(reason)}。"

    if state == "暂停" or company.pause_reason is not None:
        status = f"{status}（{ExplainCompanySyncPause(company.pause_reason)}）"
    heading = f"公司同步：{status}，范围 {len(company.scope.codes):,} 股"
    if state == "暂停" and company.round_id:
        heading += "，断点已保留"
    latest = company.latest
    if latest is None:
        lines = [heading, _source_summary(company), _formal_summary(company)]
    else:
        population = len(company.scope.codes)
        lines = [heading, f"最新字段检查截止 {company.scope.cutoff.isoformat(timespec='seconds')}。",
                 f"取得最新来源报告 {latest.financial_observed_stocks:,}/{population:,} 股，"
                 f"D25正式可读 {latest.financial_published_stocks:,} 股，来源空值 {latest.financial_missing_values:,} 项。"]
        for label, batch in (("质押", latest.pledge), ("解禁", latest.unlock)):
            if batch is not None:
                lines.append(f"{label}来源已接纳 {batch.accepted_pages}/{batch.expected_pages} 页，"
                             f"{batch.accepted_rows:,} 条，正式字段按各自凭据核验。")
        lines.append("新增公告检查" + ("完成" if latest.announcement_complete else "待续") + "，年度补采断点保留。")
    failures: list[str] = []
    pending: list[str] = []
    for family in company.families:
        step = family.result
        label = _SUMMARY_FAMILIES.get(family.family.value, _LABELS.get(family.family.value, family.family.value))
        if step.failure is not None:
            failures.append(f"{label}：{_summary_failure(step.failure)}")
        if step.pending_count:
            pending.append(f"{label} {step.pending_count:,} 项")
        elif step.failure is None and not step.business_complete and latest is None:
            pending.append(label)
    if failures:
        lines.append("异常：" + "。".join(failures) + "。")
    if pending and not result.cancelled:
        label = "仍待补证" if latest is not None and latest.ready else "待处理"
        lines.append(label + "：" + "、".join(pending) + "。")
    lines.append(_request_totals(company))
    return "\n".join(lines)


def _source_summary(company: CompanySyncRoundResult) -> str:
    steps = {
        family.family: family.result for family in company.families
        if family.family is not CompanySyncFamily.FIELDS
    }
    if not steps:
        return "来源库：本轮采集状态未核对。"
    sources = (
        f"{_SUMMARY_FAMILIES.get(family.value, _LABELS[family.value])}{_source_status(steps.get(family))}"
        for family in company.scope.families if family is not CompanySyncFamily.FIELDS
    )
    return "来源库：" + "、".join(sources) + "。"


def _source_status(step: CompanySyncStepResult | None) -> str:
    if step is None:
        return "未核对"
    if step.failure in {"source_attempt_unconfirmed", "previous_directory_attempt_unconfirmed"}:
        return "原请求待核"
    if step.failure is not None:
        return "有异常"
    if "source_budget_exhausted" in step.gaps:
        return "请求额度用尽"
    if step.processed_scope_complete:
        if "population_members_became_ineligible" in step.gaps:
            return "部分股票资格已变化"
        if "announcement_required_material_not_fully_verified" in step.gaps:
            return "必要材料待核"
        return "采集处理结束"
    return "待续传" if step.can_continue else "采集待核"


def _formal_summary(company: CompanySyncRoundResult) -> str:
    if company.business_complete:
        return "正式库：本轮资料已核实。"
    if company.publication_hash:
        return "正式库：已确认发布，业务资料仍有缺口。"
    return "正式库：本轮发布状态未核对。"


def _summary_failure(code: str) -> str:
    translated = ExplainCompanyRoundReason(code) or _REASONS.get(code) or _SUMMARY_FAILURES.get(code)
    return translated or f"具体原因见上方诊断（{code}）"


def _request_totals(company: CompanySyncRoundResult) -> str:
    if tuple(family.family for family in company.families) != company.scope.families:
        return "请求账本：本轮总数未核对。"
    used = tuple(family.source_calls for family in company.families)
    unconfirmed = tuple(family.unknown_attempts for family in company.families)
    used_text = "占用数未核对" if None in used else f"已占用 {sum(value for value in used if value is not None):,} 次"
    unknown_text = "未确认数未核对" if None in unconfirmed else f"未确认 {sum(value for value in unconfirmed if value is not None):,} 次"
    return f"请求账本：{used_text}，{unknown_text}。"


def _component(kind: str | None, scope: DisclosurePartition | StructuredCompanyQuery | None = None) -> str:
    if isinstance(scope, StructuredCompanyQuery):
        return scope.component
    if isinstance(scope, DisclosurePartition):
        return "announcements"
    return _COMPONENTS.get(kind, kind) if kind else "command"


def _source(source: str) -> str:
    return _SOURCES.get(source, source)


def _reason(reason: str) -> str:
    if reason in _REASONS:
        return _REASONS[reason]
    if reason.startswith("http_"):
        return "HTTP请求未成功，状态见错误码和因果链"
    if reason.endswith(("date_filter_ignored", "outside_window", "snapshot_future")):
        return "来源返回日期超出本次允许范围"
    return "操作未成功，具体原因见错误码和因果链"


def _seconds(value: float | None) -> str:
    return "未采集" if value is None else f"{value:.3f}秒"


def _raw_input_result(result: CompanyOperationResult) -> bool:
    return result.kind == "download" or (
        result.kind == "inventory" and result.metadata is not None and result.outcome == "reused"
    )


def _boundary(value: bool | None) -> str:
    return "未确认" if value is None else "已确认" if value else "未发生"


def _endpoint(value: str) -> str:
    if not value:
        return "未采集"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "未采集（接口名称无效）"
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return parsed.hostname + parsed.path[:120]
    return value if not any(token in value for token in ("?", "#", "@", "\\", "/")) else "未采集（非安全接口名称）"


def _fields(parts: tuple[str, ...], level: ProgressLevel = ProgressLevel.NORMAL) -> tuple[ProgressField, ...]:
    return tuple(ProgressField(part, level) for part in parts)


def _message(
    component: str,
    label: str,
    status: str,
    parts: tuple[str, ...],
    *,
    level: ProgressLevel = ProgressLevel.NORMAL,
    rows: tuple[ProgressRow, ...] = (),
) -> ProgressMessage:
    state: ProgressState = "失败" if level is ProgressLevel.ERROR else "完成"
    return ProgressMessage(
        _fields((status, *parts), level), task=ProgressTask("company", component, label), state=state,
        immediate=True, compact=True, heartbeat=False, rows=rows,
    )


@dataclass(frozen=True)
class _PublishedState:
    coverage: bool
    states: tuple[CompanyDataState | None, ...]
    pending: int
    risks: int
    positives: int
    reviews: int


class CompanyResultFeedback:
    """One command's result deduplication and error recovery, independent of heartbeat cadence."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, str]] = set()
        self._recent: deque[tuple[str, str, str]] = deque()
        self._counts: Counter[str] = Counter()
        self._published: dict[str, _PublishedState | None] = {}
        self._unresolved_failures: dict[_FailureKey, tuple[DownloadFailureRecord, str]] = {}
        self._recovered: set[_BusinessKey] = set()
        self._run_id = ""
        self._error_attempts = 0
        self._diagnosed: set[tuple[str, str, str, str]] = set()
        self._errors_by_component: Counter[str] = Counter()
        self._concluded: dict[str, tuple[tuple[tuple[str, int], ...], int, int, bool]] = {}
        self._committed_during: dict[tuple[str, int], set[str]] = {}

    @property
    def has_diagnostics(self) -> bool:
        return self._error_attempts > 0

    def observe_run(self, run_id: str) -> None:
        if run_id:
            self._run_id = run_id

    def _first(self, key: tuple[str, str, str]) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._recent.append(key)
        if len(self._recent) > 4096:
            self._seen.remove(self._recent.popleft())
        return True

    def result(self, result: CompanyOperationResult) -> tuple[ProgressMessage, ...]:
        key = (result.run_id, str(result.operation_id), result.outcome)
        if not self._first(key):
            return ()
        if result.run_id:
            self._run_id = result.run_id
        component = result.component or _component(result.kind, result.scope)
        if result.kind == "publication":
            self._committed_during.pop((result.run_id, result.operation_id), None)
        document = result.metadata
        status = {
            "accepted": "发布收尾完成" if result.kind == "publication" else "入库成功" if result.kind in {"text", "ocr", "document_commit"} else "保存成功",
            "downloaded": "获取成功", "reused": "本地复用", "rejected": "未采用（旧修订已替代）",
            "empty": "保存成功（本查询无记录）", "split": "范围拆分", "ocr_queued": "转OCR",
            "unchanged": "查询未变化（未改写目录）",
        }[result.outcome]
        parts = [f"来源 {_source(result.source)}", f"运行 {result.run_id or '未提供'} 操作 {result.operation_id}"]
        if result.outcome == "reused":
            parts.append(f"执行 本地缓存；缓存原来源 {_source(result.original_source) if result.original_source else '未提供'}")
        if result.code:
            parts.append(f"股票 {result.code} {ShortProgressText(result.name, 20)}")
        parts.append(f"对象 {document.source + ':' + document.identity if document is not None else result.object_id}")
        if document is not None:
            parts.append(f"公告 {ShortProgressText(document.title, 48)} 修订 {document.digest[:12]}")
        elif result.revision:
            parts.append(f"修订 {result.revision[:12]}")
        if result.scope is not None:
            parts.append(f"窗口 {result.scope.start}～{result.scope.end} 第{result.scope.page}页")
        if result.rows is not None:
            parts.append(f"本批发布核验 {result.rows}股" if result.kind == "publication" else f"返回 {result.rows}条")
        if result.changed_rows is not None:
            parts.append(f"实际变更 {result.changed_rows}条")
        if result.endpoint:
            parts.append(f"接口 {_endpoint(result.endpoint)}")
        if result.related_codes:
            parts.append(f"涉及合格股票 {len(result.related_codes)}股")
        if result.raw_bytes is not None:
            parts.append(f"原件 {result.raw_bytes}字节")
        if result.method is not None:
            parts.append(f"提取方式 {result.method}")
        if result.text_chars is not None:
            parts.append(f"正文 {result.text_chars}字")
        if result.ocr_completed_pages is not None:
            parts.append(f"OCR已处理 {result.ocr_completed_pages}/{result.ocr_total_pages}页")
        call_label = "获取调用（含节流及解析）" if result.kind in {"structured", "directory", "download", "annual_income"} else "处理调用"
        parts.extend((f"排队 {_seconds(result.queued_seconds)}", f"{call_label} {_seconds(result.call_seconds)}"))
        if result.commit_operation_id is not None:
            parts.append(f"提交操作 {result.commit_operation_id} 落库 {_seconds(result.commit_seconds)}")
        followup = "等待核验发布"
        if _raw_input_result(result) and result.outcome in {"downloaded", "reused"}:
            followup = "原件已保存，等待文本提取"
        elif result.outcome == "rejected":
            followup = "不计本次入库成功，保留当前有效修订"
        elif result.outcome == "ocr_queued":
            followup = "正文尚未完成，等待OCR"
        elif result.outcome == "split":
            followup = "按拆分后的查询继续，不算目录覆盖完成"
        elif result.outcome == "unchanged":
            followup = "仅当前探测页未变化；完整覆盖见后续核验"
        elif result.kind in {"directory", "structured"}:
            followup = "继续当前查询；整窗覆盖见阶段结果"
        elif result.kind == "publication":
            followup = "本次发布调用完成，逐股正式提交以实际回执为准"
        parts.append("后续 " + followup)
        self._counts[f"{component}:{result.outcome}"] += 1
        level = ProgressLevel.WARNING if result.outcome in {"rejected", "ocr_queued", "split"} else ProgressLevel.SUCCESS
        messages = [_message(component, _LABELS.get(component, result.kind), status, tuple(parts), level=level)]
        if result.outcome in {"accepted", "empty", "downloaded", "reused"}:
            messages.extend(self._resolve(result))
        return tuple(messages)

    def _resolve(self, result: CompanyOperationResult) -> tuple[ProgressMessage, ...]:
        candidates = []
        for key, (record, _component_name) in self._unresolved_failures.items():
            same_object = key[:3] == (result.source, result.object_id, result.revision)
            if same_object and (not _raw_input_result(result) or record.context.operation_kind == "download"):
                candidates.append((key, record))
        if not candidates:
            return ()
        for key, _record in candidates:
            del self._unresolved_failures[key]
        business = (result.source, result.object_id, result.revision)
        if not any(key[:3] == business for key in self._unresolved_failures):
            self._recovered.add(business)
        operations = ",".join(str(record.context.operation_id or "未采集") for _key, record in candidates)
        return (_message(result.component or _component(result.kind, result.scope), "错误恢复", "恢复成功", (
            f"来源 {_source(result.source)}", f"对象 {result.object_id}",
            f"运行 {result.run_id or '未提供'} 本次操作 {result.operation_id} 关联错误操作 {operations}",
            "已恢复到 " + ("发布调用正常完成，逐股提交以实际回执为准" if result.kind == "publication" else
                            "原件已保存，正文仍待处理" if _raw_input_result(result) else
                            "本次资料已接纳，正式发布另行确认"),
        ), level=ProgressLevel.SUCCESS),)

    def diagnostic(
        self, observation: DownloadFailureObservation, stocks: Mapping[str, CompanyStockProgress],
    ) -> tuple[ProgressMessage, ...]:
        record = observation.record
        context = record.context
        self._run_id = record.run_id
        identity = str(context.operation_id) if context.operation_id is not None else f"{record.occurred_at.isoformat()}:{context.stage}:{context.object_id}:{context.code}"
        if not self._first((record.run_id, identity, observation.persistence)):
            return ()
        component = context.component or _ERROR_COMPONENTS.get(context.stage, context.stage.removeprefix("company_"))
        if component not in _LABELS:
            component = _component(context.operation_kind)
        if observation.persistence != "pending":
            parts: tuple[str, ...] = (f"运行 {record.run_id} 操作 {context.operation_id or '未采集'}",)
            if observation.persistence == "failed":
                parts += ("原始错误保留在终端", f"诊断保存失败 {observation.recording_diagnostic}")
            return (_message(component, "诊断保存", "已保存" if observation.persistence == "saved" else "保存失败", parts,
                             level=ProgressLevel.NORMAL if observation.persistence == "saved" else ProgressLevel.ERROR),)
        self._error_attempts += 1
        self._errors_by_component[component] += 1
        business = (context.supplier, context.object_id or context.code or context.stage, context.revision)
        self._unresolved_failures[(*business, context.operation_kind or context.stage)] = (record, component)
        self._diagnosed.add((component, context.object_id, context.revision, record.reason))
        self._recovered.discard(business)
        stock = stocks.get(context.code)
        name = context.name or (stock.name if stock is not None else "")
        title = (f"公告 {ShortProgressText(context.title, 48)}",) if context.title else ()
        object_parts = (
            f"对象 {context.object_id or '未采集'}", f"修订 {context.revision[:12] or '未采集'}", *title,
            f"请求窗口 {context.window_start or '未采集'}～{context.window_end or '未采集'}",
            f"市场 {context.market or '不适用'} 页码 {context.page or '未采集'} 每页 {context.page_size or '未采集'}",
            f"接口 {_endpoint(context.endpoint)}",
        )
        cause_parts = (f"原因 {_reason(record.reason)}", f"错误码 {record.reason}",
                       f"因果链 {record.causes}", f"位置与校验 {record.diagnostic}")
        request = context.operation_kind in {"structured", "directory", "download", "annual_income"}
        local = context.operation_kind in {"text", "ocr", "document_commit", "collection_commit", "publication",
                                           "inventory", "prepare", "finish"}
        timing_parts = (f"排队 {_seconds(context.queued_seconds)}",
                        f"{'获取调用（含节流及解析）' if request else '操作调用'} {_seconds(context.elapsed_seconds)}",
                        f"超时上限 {_seconds(context.timeout_seconds)}",
                        "物理HTTP重试次数 未采集" if request else "物理HTTP请求 不适用" if local else "物理HTTP信息 未采集")
        state_parts = tuple(f"{label} {_boundary(value)}" for label, value in (
            ("原件保存", context.raw_saved), ("正文提取", context.text_extracted),
            ("资料提交", context.collection_committed), ("正式发布", context.publication_committed),
        ))
        if context.operation_id is not None:
            confirmed = self._committed_during.pop((record.run_id, context.operation_id), set())
            if confirmed:
                state_parts += (f"本操作内已确认正式提交 {len(confirmed)}股；整次发布调用尚未确认完成",)
        return (_message(component, _ACTIONS.get(context.operation_kind, "失败诊断"), "失败", (
            f"来源 {_source(context.supplier)}", f"股票 {context.code or '不适用'} {ShortProgressText(name, 20)}",
            *(("执行 本地SQLite",) if context.endpoint.startswith("sqlite:") else ()),
            f"运行 {record.run_id} 操作 {context.operation_id or '未采集'}", f"实际操作 {context.stage}",
            f"发生时间 {record.occurred_at.isoformat(timespec='seconds')}",
        ), level=ProgressLevel.ERROR, rows=(
            ProgressRow("错误对象", _fields(object_parts)), ProgressRow("错误原因", _fields(cause_parts, ProgressLevel.ERROR)),
            ProgressRow("错误耗时", _fields(timing_parts)), ProgressRow("数据状态", _fields(state_parts)),
            ProgressRow("恢复状态", _fields(("诊断保存 待确认", "恢复决定及登记结果 待确认"))),
        )),)

    def diagnosed(self, component: str, reason: str, object_id: str = "", revision: str = "") -> bool:
        return any(
            known_component == component and (
                bool(object_id) and known_object in {object_id, object_id.partition(":")[2]}
                and (not revision or known_revision == revision)
                or not object_id and known_reason == reason
            )
            for known_component, known_object, known_revision, known_reason in self._diagnosed
        )

    def recovery(self, decision: CompanyRecoveryDecision) -> tuple[ProgressMessage, ...]:
        if decision.decision == "ocr_queued" and (decision.run_id, str(decision.operation_id), "ocr_queued") in self._seen:
            return ()
        key = (decision.run_id, f"{decision.operation_id}:{decision.object_id}", decision.decision + ":" + decision.reason)
        if not self._first(key):
            return ()
        parts = [f"来源 {_source(decision.source)}", f"对象 {decision.object_id}",
                 f"运行 {decision.run_id or '未提供'} 操作 {decision.operation_id or '未采集'}",
                 f"原因 {_reason(decision.reason)}", f"错误码 {decision.reason}"]
        if decision.retry_at is not None and decision.decision in {"retry", "cooldown", "source_deferred"}:
            parts.append(f"最早重试 {decision.retry_at.isoformat(timespec='seconds')}")
        if decision.secondary:
            parts.append(f"登记错误 {decision.secondary}；原始错误保留")
        parts.append("同内容同策略不再自动重试" if decision.decision == "unextractable" else
                     "继续其它可处理资料" if decision.decision == "source_deferred" else
                     "下次沿已确认断点续传，已保存输入保留" if decision.decision == "cooldown" else "后续依据当前决定执行")
        return (_message(decision.component or _component(decision.kind, decision.scope), "恢复决定", _RECOVERIES[decision.decision], tuple(parts),
                         level=ProgressLevel.ERROR if decision.decision == "recording_failed" else ProgressLevel.WARNING),)

    def publication(
        self, receipt: CompanyPublicationProgress, stocks: Mapping[str, CompanyStockProgress], *,
        operation_id: int | None = None,
    ) -> tuple[ProgressMessage, ...]:
        if receipt.state != "committed":
            return ()
        if receipt.publication_id and not self._first((self._run_id, receipt.publication_id, "publication")):
            return ()
        if operation_id is not None:
            self._committed_during.setdefault((self._run_id, operation_id), set()).update(receipt.codes)
        messages = []
        for code in receipt.codes:
            stock = stocks.get(code)
            state = None if stock is None else _PublishedState(stock.coverage_complete, (
                stock.financial_state, stock.pledge_state, stock.unlock_state, stock.announcement_state,
            ), stock.pending, stock.risks, stock.positives, stock.review_count)
            if code in self._published and self._published[code] == state and (
                not receipt.publication_id or code not in receipt.changed_codes
            ):
                continue
            self._published[code] = state
            parts = [f"股票 {code} {ShortProgressText(stock.name, 20) if stock is not None else ''}",
                     "来源 本地发布", f"运行 {self._run_id or '未提供'}",
                     f"发布回执 {receipt.publication_id or '未采集'}", "正式提交回执 已确认"]
            status = "已提交，覆盖状态未核验"
            if stock is not None and state is not None:
                status = "已提交，完整覆盖" if stock.coverage_complete else "已提交，部分覆盖"
                parts.extend(f"{label}{_state(value)}" for label, value in zip(
                    ("财务", "质押", "解禁", "公告"), state.states, strict=True,
                ))
                parts.extend((f"正文已分析 {stock.analyzed}/{stock.announcements}篇，待补 {stock.pending}篇",
                              f"风险 {stock.risks}条 利好 {stock.positives}条 待核实 {stock.review_count}项",
                              "事实复用，保留原来源" if stock.reused else "本轮事实已提交",
                              "后续 沿原断点补缺" if not stock.coverage_complete else "本股核验资料完整"))
            messages.append(_message("publish", "股票发布", status, tuple(parts), level=ProgressLevel.SUCCESS))
        return tuple(messages)

    def is_new_publication(self, receipt: CompanyPublicationProgress) -> bool:
        return not receipt.publication_id or receipt.state != "committed" or (
            self._run_id, receipt.publication_id, "publication",
        ) not in self._seen

    def summary(self) -> tuple[str, ...]:
        unresolved_objects = {key[:3] for key in self._unresolved_failures}
        parts = [f"本轮错误 {self._error_attempts}次，已恢复 {len(self._recovered)}个对象，本轮仍未恢复 {len(unresolved_objects)}个对象"]
        if self._counts:
            outcomes = {"accepted": "接纳", "empty": "无记录", "reused": "复用", "rejected": "未采用",
                        "downloaded": "获取成功", "split": "范围拆分", "ocr_queued": "转OCR", "unchanged": "查询未变化"}
            parts.append("本轮确认结果 " + "、".join(
                f"{_LABELS.get(key.split(':')[0], key.split(':')[0])}/{outcomes[key.split(':')[1]]} {count}次"
                for key, count in sorted(self._counts.items())
            ))
        return tuple(parts)

    def conclude(self, component: str, *, incomplete: bool = False) -> tuple[ProgressMessage, ...]:
        counts = {key.partition(":")[2]: count for key, count in self._counts.items()
                  if key.partition(":")[0] == component}
        unresolved = len({key[:3] for key, (_record, owner) in self._unresolved_failures.items() if owner == component})
        signature = tuple(sorted(counts.items())), self._errors_by_component[component], unresolved, incomplete
        if self._concluded.get(component) == signature or not counts and not self._errors_by_component[component]:
            return ()
        self._concluded[component] = signature
        labels = {"accepted": "接纳", "empty": "无记录", "reused": "缓存复用", "rejected": "旧修订未采用",
                  "downloaded": "原件获取", "split": "范围拆分", "ocr_queued": "转OCR", "unchanged": "查询未变化"}
        parts = tuple(f"{labels[outcome]} {count}次" for outcome, count in sorted(counts.items()))
        parts += (f"本轮错误 {self._errors_by_component[component]}次", f"本轮尚未恢复 {unresolved}个对象",
                  "次数为业务结果，不代表物理HTTP次数；待补数量见总览")
        return (_message(component, _LABELS.get(component, component) + "结果",
                         "阶段结束，保留未完成项" if incomplete else "阶段确认结果", parts,
                         level=ProgressLevel.WARNING if incomplete else ProgressLevel.NORMAL),)

    def confirm_component(self, component: str) -> tuple[ProgressMessage, ...]:
        confirmed = [(key, record) for key, (record, owner) in self._unresolved_failures.items() if owner == component]
        if not confirmed:
            return ()
        for key, _record in confirmed:
            del self._unresolved_failures[key]
            if not any(other[:3] == key[:3] for other in self._unresolved_failures):
                self._recovered.add(key[:3])
        operations = ",".join(str(record.context.operation_id or "未采集") for _key, record in confirmed[:10])
        return (_message(component, "恢复核验", "本阶段确认完成，前述操作错误已恢复", (
            f"运行 {self._run_id or '未提供'}", f"关联错误操作 {operations}",
            f"确认恢复 {len({key[:3] for key, _record in confirmed})}个对象",
            "当前组件任务已确认；逐股资料完整度和正式发布另列",
        ), level=ProgressLevel.SUCCESS),)

    def conclude_remaining(self, *, incomplete: bool = False) -> tuple[ProgressMessage, ...]:
        components = tuple(dict.fromkeys((*[key.partition(":")[0] for key in self._counts], *self._errors_by_component)))
        return tuple(message for component in components for message in self.conclude(component, incomplete=incomplete))


def _state(value: CompanyDataState | None) -> str:
    return "待核验" if value is None else {
        CompanyDataState.COMPLETE: "完整", CompanyDataState.PARTIAL: "部分", CompanyDataState.UNAVAILABLE: "缺失",
    }[value]


__all__ = [
    "BuildCompanyRoundFinishMessage",
    "CompanyResultFeedback",
    "ExplainCompanyRoundReason",
    "ExplainCompanySyncPause",
    "FormatCompanySyncSummary",
    "ProjectCompanyRoundFamilyStatus",
]
