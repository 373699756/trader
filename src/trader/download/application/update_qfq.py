"""Bounded supplier waves with resumable, single-writer qfq publication."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import islice
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import (
    QfqDownloadProgress,
    QfqFailureCode,
    QfqMessage,
    QfqStockFeedback,
)
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_price_qualification import QualifyReplacementWindow
from trader.download.domain.price_repair import PriceIssue
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.download.domain.price_review import PriceReviewBudget, PriceReviewBudgetExhausted, PriceReviewExhaustionReason, PriceReviewRequestBudget
from trader.download.domain.qfq_planning import QfqNonTradingFact, QfqPlanningSnapshot, QfqPlanningState
from trader.download.domain.qfq_window import (
    CompletedDailyCutoff,
    MergeDailyTail,
    QfqUpdateResult,
    QfqWindowConfiguration,
    QfqWindowSnapshot,
    QfqPendingReason,
    QfqPriceReviewSummary,
)
from trader.infra.workers import SubmitWithBackpressure, WorkerExecutor, WorkerResourceRejectedError
from trader.download.application.download_failure import DownloadFailurePort
from trader.download.domain.download_failure import DownloadFailureContext


class QfqPairPublicationError(RuntimeError):
    """A later board failed after earlier paired transactions committed."""

    def __init__(
        self,
        codes: tuple[str, ...],
        files: tuple[str, ...],
        rows: int,
        *,
        reason: str = "qfq_pair_publication_failed",
    ) -> None:
        super().__init__(reason)
        self.codes = codes
        self.files = files
        self.rows = rows


class QfqWindowPort(Protocol):
    def publication(self, peer: QfqWindowPort) -> AbstractContextManager[None]: ...

    def repair_cutoff(self) -> date | None: ...

    def price_issues(self, codes: frozenset[str] | None = None, dates: tuple[date, ...] | None = None) -> tuple[PriceIssue, ...]: ...

    def record_price_issue(self, issue: PriceIssue, observed: PublishedHistoryWindow | None = None) -> None: ...

    def repair_reference(self, code: str) -> PublishedHistoryWindow: ...

    def covered_dates(self, code: str) -> tuple[date, ...]: ...

    def reserve_repair(self, issue: PriceIssue, batch: str, *, explicit: bool) -> bool: ...

    def fail_repair(self, code: str, batch: str, reason: str) -> None: ...

    def source_identity(self, code: str) -> str | None: ...

    def replace_paired_windows(
        self,
        peer: QfqWindowPort,
        windows: tuple[PublishedHistoryWindow, ...],
        source_identity: str,
        configuration: QfqWindowConfiguration,
        *,
        calendar: tuple[date, ...],
        completed_day: date | None,
        nontrading_facts: tuple[QfqNonTradingFact, ...] | None = None,
        review_budget: PriceReviewRequestBudget | None = None,
    ) -> tuple[tuple[str, ...], int]: ...

    def completed(self, day: date, code: str, source: str) -> bool: ...

    def read_code(self, code: str, *, review_budget: PriceReviewRequestBudget | None = None) -> PublishedHistoryWindow: ...

    def read_codes(self, codes: tuple[str, ...], *, review_budget: PriceReviewRequestBudget | None = None) -> tuple[PublishedHistoryWindow, ...]: ...

    def inspect_windows(self, allowed_codes: frozenset[str]) -> QfqWindowSnapshot: ...

    def planning_snapshot(self, allowed_codes: frozenset[str], *, batched: bool = False) -> QfqPlanningSnapshot: ...

    def review_states(self, allowed_codes: frozenset[str], *, review_budget: PriceReviewRequestBudget | None = None) -> tuple[QfqPlanningState, ...]: ...

    def select_published_codes(
        self, peer: QfqWindowPort, allowed_codes: frozenset[str]
    ) -> tuple[tuple[str, ...], int]: ...


class QfqSupplierPort(Protocol):
    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...], *, request_budget: PriceReviewRequestBudget | None = None) -> PublishedHistoryWindow: ...


class QfqRepairSupplierPort(Protocol):
    def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...


@dataclass(frozen=True)
class _DownloadPlan:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    reuse_previous: bool
    observed_on: date
    source: str
    nontrading: tuple[QfqNonTradingFact, ...] = ()
    baostock_basis: bool = False
    recheck_due: bool = False
    review_requested: bool = False
    review_only: bool = False


@dataclass(frozen=True)
class _DownloadJob:
    security: BaoStockSecurity
    dates: tuple[date, ...]
    previous: PublishedHistoryWindow
    source: str
    baostock_basis: bool = False
    recheck_due: bool = False
    observed_on: date | None = None
    nontrading: tuple[QfqNonTradingFact, ...] = ()
    review_requested: bool = False
    review_only: bool = False


@dataclass(frozen=True)
class _DownloadedWindow:
    window: PublishedHistoryWindow
    nontrading: tuple[QfqNonTradingFact, ...]

    @property
    def code(self) -> str:
        return self.window.code

    @property
    def cells(self) -> tuple[PublishedHistoryCell, ...]:
        return self.window.cells


class _BaoStockCooldown(RuntimeError):
    def __init__(self) -> None:
        super().__init__("price_source_basis_conflict")


@dataclass(frozen=True)
class _WriteReceipt:
    codes: tuple[str, ...]
    files: tuple[str, ...]
    rows: int
    failure: RuntimeError | OSError | ValueError | None = None
    write_seconds: float = 0.0


@dataclass(frozen=True)
class UpdateQfqWindows:
    v2: QfqWindowPort
    v3: QfqWindowPort
    supplier: QfqSupplierPort
    cancel_requested: Callable[[], bool]
    report: Callable[[ProgressMessage], None]
    configuration: QfqWindowConfiguration
    workers: int | None = None
    worker_pool: WorkerExecutor | None = None
    writer_pool: WorkerExecutor | None = None
    monotonic: Callable[[], float] = monotonic
    repair_supplier: QfqRepairSupplierPort | None = None
    repair_only: bool = False
    failures: DownloadFailurePort | None = None
    price_review_budget: PriceReviewBudget | None = None
    batch_planning_candidate: bool = False
    skip_cooling_tencent_probe: bool = False

    def __post_init__(self) -> None:
        workers = self.configuration.workers if self.workers is None else self.workers
        if not 1 <= workers <= 12:
            raise ValueError("qfq workers must be within 1..12")
        object.__setattr__(self, "workers", workers)

    @property
    def worker_count(self) -> int:
        if self.workers is None:
            raise RuntimeError("qfq workers were not initialized")
        return self.workers

    def _record_failure(self, stage: str, exc: BaseException, code: str = "", elapsed: float | None = None) -> None:
        if self.failures is not None and not self.cancel_requested():
            self.failures(DownloadFailureContext(stage, code=code, elapsed_seconds=elapsed), exc)

    def seed(
        self, windows: Iterable[PublishedHistoryWindow], identity: str, *, calendar: tuple[date, ...],
        nontrading_observed_on: date | None = None,
    ) -> QfqUpdateResult:
        if nontrading_observed_on is not None and type(nontrading_observed_on) is not date:
            raise ValueError("qfq_seed_observation_date_invalid")
        with self._publication_check():
            return self._seed(windows, identity, calendar=calendar, nontrading_observed_on=nontrading_observed_on)

    def _seed(
        self, windows: Iterable[PublishedHistoryWindow], identity: str, *, calendar: tuple[date, ...],
        nontrading_observed_on: date | None,
    ) -> QfqUpdateResult:
        changed: set[str] = set()
        rows = 0
        count = 0
        for window in windows:
            if self.cancel_requested():
                break
            bounded = PublishedHistoryWindow(
                window.code,
                tuple(
                    cell
                    for cell in window.cells
                    if cell.trade_date in calendar[-self.configuration.maximum_sessions :]
                ),
            )
            files, delta = self.v2.replace_paired_windows(
                self.v3,
                (bounded,),
                identity,
                self.configuration,
                calendar=calendar[-self.configuration.maximum_sessions :],
                completed_day=None,
                nontrading_facts=self._observed_nontrading(bounded, identity, nontrading_observed_on) if nontrading_observed_on is not None else None,
            )
            changed.update(files)
            rows += delta
            count += 1
            if count % 64 == 0:
                self.report(
                    QfqMessage(
                        "历史提取",
                        f"已处理 {count} 只 | 变更文件 {len(changed)}",
                    )
                )
        return QfqUpdateResult(None, count, changed_files=tuple(sorted(changed)), changed_rows=rows, price_review=QfqPriceReviewSummary(discovery_completed=False))

    def execute(self, observed_at: datetime) -> QfqUpdateResult:
        with self._publication_check():
            progress, failure_reason = self._execute(observed_at)
        return progress.finish(self.cancel_requested(), failure_reason=failure_reason)

    @contextmanager
    def _publication_check(self) -> Iterator[None]:
        with self.v2.publication(self.v3):
            yield
            self.report(QfqMessage("数据库文件校验", "主文件及WAL整体指纹", state="开始"))
        self.report(QfqMessage(
            "数据库文件校验",
            "主文件及WAL整体指纹已完成", state="完成", finished=True,
        ))

    def _execute(
        self, observed_at: datetime
    ) -> tuple[QfqDownloadProgress, Literal["qfq_worker_unavailable"] | None]:
        started = self.monotonic()
        self.report(QfqMessage("准备", "正在加载沪深股票名单和 Tencent 交易日历", state="开始"))
        try:
            cutoff = self.v2.repair_cutoff() if self.repair_only else None
            context = self.supplier.load_qfq_context(
                cutoff or CompletedDailyCutoff(observed_at), self.configuration.maximum_sessions
            )
        except (RuntimeError, OSError, ValueError) as exc:
            cancelled = self.cancel_requested()
            self._record_failure("loading_context", exc, elapsed=self.monotonic() - started)
            stage = "已取消" if cancelled else "准备失败"
            self.report(
                QfqMessage(
                    stage,
                    QfqFailureCode(exc),
                    level=ProgressLevel.WARNING if cancelled else ProgressLevel.ERROR,
                )
            )
            raise
        source = f"{context.source_versions.sdk_version}:{context.source_versions.content_hash}"
        self.report(QfqMessage("准备", f"总体 {len(context.universe)} 股 | 交易日 {len(context.calendar.open_dates)}日",
                               state="完成", finished=True))
        progress = QfqDownloadProgress(
            context.calendar.open_dates[-1],
            source,
            len(context.universe),
            self.report,
            price_review=QfqPriceReviewSummary(discovery_completed=False),
        )
        allowed_codes = frozenset(security.code for security in context.universe)
        files, changed_rows = self.v2.select_published_codes(self.v3, allowed_codes)
        progress.changed.update(files)
        progress.rows += changed_rows
        progress.publish("本地窗口检查", "核对两档窗口日期与来源", state="开始")
        planning = self.v2.planning_snapshot(allowed_codes, batched=self.batch_planning_candidate)
        v2_snapshot = planning.windows
        v3_snapshot = (
            self.v3.planning_snapshot(allowed_codes, batched=True).windows
            if self.batch_planning_candidate else self.v3.inspect_windows(allowed_codes)
        )
        progress.publish(
            "本地窗口检查",
            f"V2 {len(v2_snapshot.states)} 只 | V3 {len(v3_snapshot.states)} 只",
            state="完成", finished=True,
        )
        progress.publish("就绪", f"股票 {progress.total} 只 | 截止 {progress.day} | 并发 {self.worker_count}",
                         state="完成", finished=True)
        if self.repair_only:
            # Older files had no durable issue inventory. Recover absent dates
            # only for locally published stocks; never schedule the whole universe.
            for security in context.universe:
                state = v2_snapshot.state_for(security.code)
                if state is not None and state.verified:
                    covered = self.v2.covered_dates(security.code)
                    absent = tuple(day for day in context.calendar.expected_dates(security) if day <= max(covered) and day not in covered) if covered else ()
                    if absent:
                        self.v2.record_price_issue(PriceIssue(security.code, absent, "legacy_window_missing"))
            failure_reason = None
        else:
            plans = iter(self._plans(context, progress, planning, v3_snapshot, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()))
            failure_reason = self._download_rolling(
                plans, progress, context.calendar.open_dates[-self.configuration.maximum_sessions :]
            )
        if self.repair_supplier is not None and failure_reason is None and not self.cancel_requested():
            progress.publish("BaoStock补缺", "开始读取当前缺口", state="开始")
            self._repair(context, progress, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date())
            progress.publish("BaoStock补缺", "本轮补缺阶段结束，最终覆盖见汇总", state="完成", finished=True)
        if not self.repair_only and failure_reason is None and not self.cancel_requested():
            progress.publish("停牌复核", "沿用本次请求预算", state="开始")
            progress.price_review = self._review_persisted(context, progress, observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date())
            progress.publish("停牌复核", "本轮复核结束，未确认事实保留", state="完成", finished=True)
        return progress, failure_reason

    def _review_persisted(self, context: HistorySupplierContext, progress: QfqDownloadProgress, observed_on: date) -> QfqPriceReviewSummary:
        """Review only still-due facts, after ordinary requests have refreshed them."""
        budget = self.price_review_budget
        if budget is None:
            return QfqPriceReviewSummary(discovery_completed=False)
        requests_before = budget.request_count
        examined = 0
        exhaustion: PriceReviewExhaustionReason | None = None
        try:
            if budget.request_count >= self.configuration.price_review.maximum_requests:
                return QfqPriceReviewSummary(exhaustion_reason="request_limit", discovery_completed=False)
            budget.check_deadline()
            plans, remaining_codes = self._review_candidates(context, progress, observed_on, budget)
        except PriceReviewBudgetExhausted as exc:
            progress.publish("停牌复核", "发现阶段预算耗尽 | 原事实保留待复核", level=ProgressLevel.WARNING)
            return QfqPriceReviewSummary(exhaustion_reason=exc.reason, discovery_completed=False)
        except (RuntimeError, OSError, ValueError) as exc:
            self._record_failure("local_review_inventory", exc)
            progress.publish("停牌复核", f"发现未完成 | {QfqFailureCode(exc)}", level=ProgressLevel.WARNING)
            return QfqPriceReviewSummary(discovery_completed=False)
        finally:
            budget.pause()
        remaining = set(remaining_codes)
        for plan in plans:
            if self.cancel_requested():
                break
            try:
                if not budget.reserve_code(plan.security.code):
                    exhaustion = budget.exhausted_reason
                    progress.publish("停牌复核", f"预算已用尽 | 剩余事实保留待下次复核 | {budget.exhausted_reason}", level=ProgressLevel.WARNING)
                    if exhaustion == "code_limit":
                        continue
                    break
                examined += 1
                job = self._hydrate_wave((plan,), review_budget=budget)[0]
                budget.check_deadline()
                downloaded = self._download(job)
                budget.check_deadline()
                receipt = self._write_downloaded(((job, downloaded),), progress.source, context.calendar.open_dates[-self.configuration.maximum_sessions:], review_budget=budget)
                if receipt.failure is not None:
                    progress.publish("停牌复核", f"{plan.security.code} | 发布未完成 | 保留待复核", level=ProgressLevel.WARNING)
                    continue
                progress.changed.update(receipt.files)
                progress.rows += receipt.rows
                if not any(fact.kind == "inferred" for fact in downloaded.nontrading):
                    remaining.discard(plan.security.code)
                if job.previous.cells and job.previous.cells[-1].trade_date == progress.day:
                    if job.previous.cells[-1].status == "tencent_inferred_suspended":
                        progress.latest_inferred_suspended = max(0, progress.latest_inferred_suspended - 1)
                    elif job.previous.cells[-1].status == "supplier_marked_suspended":
                        progress.latest_suspended = max(0, progress.latest_suspended - 1)
                progress.record_latest(downloaded.window)
                progress.publish("停牌复核", f"{plan.security.code} | 来源已复核 | 请求 {budget.request_count}")
            except PriceReviewBudgetExhausted as exc:
                exhaustion = exc.reason
                progress.publish("停牌复核", f"{plan.security.code} | 预算耗尽 | 保留原事实待复核", level=ProgressLevel.WARNING)
                break
            except (RuntimeError, OSError, ValueError) as exc:
                self._record_failure("tencent_review", exc, plan.security.code)
                progress.publish("停牌复核", f"{plan.security.code} | 未确认 | {QfqFailureCode(exc)}", level=ProgressLevel.WARNING)
            finally:
                budget.pause()
        return QfqPriceReviewSummary(
            examined, budget.request_count - requests_before, len(remaining), exhaustion, discovery_completed=True,
        )

    def _review_candidates(
        self, context: HistorySupplierContext, progress: QfqDownloadProgress, observed_on: date,
        budget: PriceReviewRequestBudget,
    ) -> tuple[tuple[_DownloadPlan, ...], frozenset[str]]:
        states = self.v2.review_states(frozenset(item.code for item in context.universe), review_budget=budget)
        metadata_by_code = {state.code: state for state in states}
        plans: list[_DownloadPlan] = []
        remaining: set[str] = set()
        for security in context.universe:
            budget.check_deadline()
            metadata = metadata_by_code.get(security.code)
            if metadata is None or metadata.source not in (progress.source, f"baostock:{progress.source}"):
                continue
            dates = context.calendar.expected_dates(security)[-self.configuration.maximum_sessions:]
            facts = tuple(fact for fact in metadata.nontrading if fact.trade_date in dates and fact.source == metadata.source and fact.kind != "legacy_unverified")
            if any(fact.kind == "inferred" for fact in facts):
                remaining.add(security.code)
            if metadata.completed_day != progress.day or metadata.completed_source != metadata.source:
                continue
            inferred_due = any(fact.kind == "inferred" and fact.checked_on is not None and sum(day > fact.checked_on for day in context.calendar.open_dates) >= self.configuration.price_review.interval_sessions for fact in facts)
            basis = metadata.source == f"baostock:{progress.source}"
            basis_due = basis and (metadata.tencent_checked_on is None or sum(day > metadata.tencent_checked_on for day in context.calendar.open_dates) >= 20)
            if inferred_due or basis_due:
                plans.append(_DownloadPlan(security, dates, True, observed_on, progress.source, facts, basis, basis and (inferred_due or basis_due), True, True))
        budget.check_deadline()
        return tuple(plans), frozenset(remaining)

    def _repair(self, context: HistorySupplierContext, progress: QfqDownloadProgress, observed_on: date) -> None:
        assert self.repair_supplier is not None
        batch = uuid4().hex
        securities = {item.code: item for item in context.universe}
        issues = self.v2.price_issues(frozenset(securities), context.calendar.open_dates)
        repair_states = {state.code: state for state in self.v2.review_states(frozenset(issue.code for issue in issues))}
        if self.repair_only:
            progress.total = len(issues)
        progress.publish("BaoStock补缺", f"问题股票 {len(issues)} 只 | 串行请求 | 最小间隔 1.5 秒")
        for issue in issues:
            if self.cancel_requested():
                break
            security = securities.get(issue.code)
            if security is None:
                continue
            dates = context.calendar.expected_dates(security)[-self.configuration.maximum_sessions :]
            issue = PriceIssue(issue.code, tuple(day for day in issue.dates if day in dates), issue.reason)
            if not issue.dates or not self.v2.reserve_repair(issue, batch, explicit=self.repair_only):
                continue
            repair_started = self.monotonic()
            try:
                previous = self.v2.read_code(issue.code)
                state = repair_states.get(issue.code)
                source = f"baostock:{progress.source}"
                prior_facts = tuple(fact for fact in state.nontrading if fact.source == source and fact.kind != "legacy_unverified" and fact.trade_date in dates) if state is not None and state.source == source else ()
                if prior_facts:
                    cells = {cell.trade_date: cell for cell in previous.cells}
                    cells.update((fact.trade_date, fact.cell()) for fact in prior_facts)
                    previous = PublishedHistoryWindow(issue.code, tuple(cells[day] for day in sorted(cells)))
                candidate = None
                observed_dates = dates
                if state is not None and state.source == source and previous.cells:
                    missing = tuple(day for day in dates if day not in {cell.trade_date for cell in previous.cells})
                    if len(missing) <= 5:
                        observed_dates = dates[-max(5, len(missing) + 3):]
                        tail = self.repair_supplier.fetch_baostock_window(security, observed_dates)
                        candidate = MergeDailyTail(previous, tail, dates)
                        if candidate is not None and tuple(cell.trade_date for cell in candidate.cells) != dates:
                            candidate = None
                if candidate is None:
                    observed_dates = dates
                    candidate = self.repair_supplier.fetch_baostock_window(security, dates)
                candidate = QualifyReplacementWindow(dates, self.v2.repair_reference(issue.code), candidate)
                files, rows = self.v2.replace_paired_windows(
                    self.v3,
                    (candidate,),
                    source,
                    self.configuration,
                    calendar=context.calendar.open_dates[-self.configuration.maximum_sessions :],
                    completed_day=progress.day,
                    nontrading_facts=self._observed_nontrading(candidate, source, observed_on, previous=prior_facts, observed_dates=observed_dates),
                )
                progress.changed.update(files)
                progress.rows += rows
                progress.publish("BaoStock补缺", f"{issue.code} | 整窗发布成功 | {len(dates)} 股日")
            except (RuntimeError, OSError, ValueError) as exc:
                self._record_failure("baostock_repair", exc, issue.code, self.monotonic() - repair_started)
                self.v2.fail_repair(issue.code, batch, QfqFailureCode(exc))
                progress.publish("BaoStock补缺", f"{issue.code} | 未修复 | {QfqFailureCode(exc)}", level=ProgressLevel.WARNING)
        remaining = self.v2.price_issues(frozenset(securities), context.calendar.open_dates)
        # Durable state is authoritative after the second phase; Tencent pending
        # counters must not continue to report a stock that has been repaired.
        resolved = len(issues) - len(remaining)
        progress.completed += resolved
        progress.pending = len(remaining)
        reasons: dict[str, int] = {}
        for issue in remaining:
            reasons[issue.reason] = reasons.get(issue.reason, 0) + 1
        progress.pending_reasons = tuple(QfqPendingReason(reason, count) for reason, count in sorted(reasons.items()))

    def _plans(
        self,
        context: HistorySupplierContext,
        progress: QfqDownloadProgress,
        planning: QfqPlanningSnapshot,
        v3_snapshot: QfqWindowSnapshot,
        observed_on: date,
    ) -> Iterable[_DownloadPlan]:
        for security in context.universe:
            if self.cancel_requested():
                return
            dates = context.calendar.expected_dates(security)[-self.configuration.maximum_sessions :]
            try:
                plan = self._prepare_plan(security, dates, progress, planning, v3_snapshot, observed_on)
            except (RuntimeError, OSError, ValueError) as exc:
                self._record_failure("local_validation", exc, security.code)
                if self.repair_supplier is not None:
                    self.v2.record_price_issue(PriceIssue(security.code, dates, QfqFailureCode(exc)))
                progress.record_pending(security, exc, dates, action="本地检查")
                plan = None
            if plan is not None:
                yield plan

    def _prepare_plan(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
        progress: QfqDownloadProgress,
        planning: QfqPlanningSnapshot,
        v3_snapshot: QfqWindowSnapshot,
        observed_on: date,
    ) -> _DownloadPlan | None:
        if not dates:
            progress.not_applicable += 1
            progress.record_stock(
                security,
                QfqStockFeedback("无需下载", "无需下载", 0, 0, 0, 0),
            )
            return None
        v2_state = planning.windows.state_for(security.code)
        v3_state = v3_snapshot.state_for(security.code)
        metadata = planning.state_for(security.code)
        same_source = all(
            state is not None and state.verified and state.source == progress.source for state in (v2_state, v3_state)
        )
        baostock_source = all(state is not None and state.verified and state.source == f"baostock:{progress.source}" for state in (v2_state, v3_state))
        selected_source = f"baostock:{progress.source}" if baostock_source else progress.source
        facts = tuple(fact for fact in metadata.nontrading if fact.trade_date in dates and fact.source == selected_source and fact.kind != "legacy_unverified" and fact.checked_on is not None and fact.checked_on <= observed_on) if metadata else ()
        fact_dates = {fact.trade_date for fact in facts}
        complete = (
            (same_source or baostock_source)
            and metadata is not None and metadata.completed_day == progress.day and metadata.completed_source == selected_source
            and v2_state is not None
            and v3_state is not None
            and set(v2_state.dates) | fact_dates == set(dates)
            and set(v3_state.dates) | {day for day in fact_dates if day >= dates[-min(self.configuration.v3_sessions, len(dates))]}
            == set(dates[-self.configuration.v3_sessions:])
        )
        if complete and v2_state is not None:
            progress.completed += 1
            progress.skipped += 1
            progress.record_stock(
                security,
                QfqStockFeedback(
                    "本地复用",
                    "复用",
                    len(v2_state.dates),
                    len(v2_state.dates),
                    len(v2_state.dates),
                    len(v2_state.dates),
                ),
            )
            if facts and facts[-1].trade_date == dates[-1]:
                progress.record_latest(PublishedHistoryWindow(security.code, (facts[-1].cell(),)))
            return None
        basis = self.repair_supplier is not None and v2_state is not None and v2_state.source.startswith("baostock:")
        return _DownloadPlan(security, dates, same_source or baostock_source, observed_on, progress.source, facts, basis)

    def _hydrate_wave(self, plans: tuple[_DownloadPlan, ...], *, review_budget: PriceReviewRequestBudget | None = None) -> tuple[_DownloadJob, ...]:
        reusable_codes = tuple(plan.security.code for plan in plans if plan.reuse_previous or plan.baostock_basis)
        previous = (self.v2.read_codes(reusable_codes, review_budget=review_budget) if review_budget is not None else self.v2.read_codes(reusable_codes)) if reusable_codes else ()
        previous_index = 0
        jobs: list[_DownloadJob] = []
        for plan in plans:
            window = PublishedHistoryWindow(plan.security.code, ())
            if plan.reuse_previous or plan.baostock_basis:
                window = previous[previous_index]
                previous_index += 1
                if window.code != plan.security.code:
                    raise RuntimeError("qfq batch read order mismatch")
            if plan.nontrading:
                cells = {cell.trade_date: cell for cell in window.cells}
                cells.update((fact.trade_date, fact.cell()) for fact in plan.nontrading)
                window = PublishedHistoryWindow(window.code, tuple(cells[day] for day in sorted(cells)))
            jobs.append(_DownloadJob(plan.security, plan.dates, window, plan.source, plan.baostock_basis, plan.recheck_due, plan.observed_on, plan.nontrading, plan.review_requested, plan.review_only))
        return tuple(jobs)

    def _download_rolling(
        self, plans: Iterable[_DownloadPlan], progress: QfqDownloadProgress, calendar: tuple[date, ...]
    ) -> Literal["qfq_worker_unavailable"] | None:
        remaining = iter(plans)
        futures: dict[Future[_DownloadedWindow], _DownloadJob] = {}
        downloaded: list[tuple[_DownloadJob, _DownloadedWindow]] = []
        writing: Future[_WriteReceipt] | None = None
        write_batch: tuple[tuple[_DownloadJob, _DownloadedWindow], ...] = ()
        exhausted = False
        failure_reason: Literal["qfq_worker_unavailable"] | None = None
        last_feedback = last_flush = self.monotonic()
        finished_normally = False
        progress.publish("价格更新", f"来源 Tencent | 股票 {progress.total} | 并发 {self.worker_count} "
                         f"| 单次超时 {self.configuration.source_timeout_seconds}秒 | 重试上限 {self.configuration.source_retries}",
                         state="开始")
        try:
            while not self.cancel_requested():
                if writing is not None and writing.done():
                    self._accept_write(write_batch, writing.result(), progress)
                    writing, write_batch = None, ()
                buffered = len(downloaded) + len(write_batch) + len(futures)
                cells = sum(len(window.cells) for _, window in (*downloaded, *write_batch))
                cells += sum(len(job.dates) for job in futures.values())
                capacity = min(
                    self.worker_count - len(futures),
                    max(0, 32 - buffered),
                    max(0, (8192 - cells) // self.configuration.maximum_sessions),
                )
                plan_batch = tuple(islice(remaining, capacity)) if not exhausted else ()
                if capacity and len(plan_batch) < capacity:
                    exhausted = True
                if plan_batch:
                    failure_reason = self._fill_slots(plan_batch, futures, progress)
                    exhausted = exhausted or failure_reason is not None
                if (
                    downloaded
                    and writing is None
                    and (
                        len(downloaded) >= 16
                        or sum(len(window.cells) for _, window in downloaded) >= 4096
                        or self.monotonic() - last_flush >= 1
                        or not futures
                    )
                ):
                    count, batch_cells = 0, 0
                    for _, window in downloaded[:16]:
                        if count and batch_cells + len(window.cells) > 4096:
                            break
                        count, batch_cells = count + 1, batch_cells + len(window.cells)
                    write_batch = tuple(downloaded[:count])
                    del downloaded[:count]
                    writing = self._submit_write(write_batch, progress.source, calendar)
                    last_flush = self.monotonic()
                if not futures and exhausted and not downloaded and writing is None:
                    break
                if self.monotonic() - last_feedback >= 5:
                    progress.publish(
                        "价格更新",
                        f"网络在途 {len(futures)} | 待写 {len(downloaded)} | 写入 {len(write_batch)} | 已处理 {progress.processed}/{progress.total}",
                        state="等待",
                    )
                    last_feedback = self.monotonic()
                if futures or writing is not None:
                    wait(
                        (*futures, *((writing,) if writing is not None else ())), timeout=1, return_when=FIRST_COMPLETED
                    )
                if self.cancel_requested():
                    break
                downloaded.extend(
                    self._collect_completed({future for future in futures if future.done()}, futures, progress)
                )
            finished_normally = True
        finally:
            for future in futures:
                future.cancel()
            if writing is not None:
                # Never release the maintenance lock while a paired transaction is still running.
                writing.result()
            progress.publish("价格更新", f"已处理 {progress.processed}/{progress.total} | 待重试 {progress.pending}",
                             state="取消" if self.cancel_requested() else "失败" if failure_reason or not finished_normally else "完成",
                             finished=True)
        return failure_reason

    def _submit_write(
        self, batch: tuple[tuple[_DownloadJob, _DownloadedWindow], ...], source: str, calendar: tuple[date, ...]
    ) -> Future[_WriteReceipt]:
        if self.writer_pool is not None:
            return SubmitWithBackpressure(self.writer_pool, self._write_downloaded, batch, source, calendar)
        result: Future[_WriteReceipt] = Future()
        result.set_result(self._write_downloaded(batch, source, calendar))
        return result

    def _write_downloaded(
        self, batch: tuple[tuple[_DownloadJob, _DownloadedWindow], ...], source: str, calendar: tuple[date, ...], *,
        review_budget: PriceReviewRequestBudget | None = None,
    ) -> _WriteReceipt:
        if self.cancel_requested():
            return _WriteReceipt((), (), 0, RuntimeError("cancelled"))
        started = self.monotonic()
        try:
            files, rows = self.v2.replace_paired_windows(
                self.v3,
                tuple(window.window for _, window in batch),
                source,
                self.configuration,
                calendar=calendar,
                completed_day=calendar[-1],
                nontrading_facts=tuple(fact for _, window in batch for fact in window.nontrading),
                review_budget=review_budget,
            )
            return _WriteReceipt(
                tuple(window.code for _, window in batch), files, rows, write_seconds=self.monotonic() - started
            )
        except PriceReviewBudgetExhausted:
            raise
        except QfqPairPublicationError as exc:
            if review_budget is None:
                for job, _window in batch:
                    self._record_failure("paired_publication", exc, job.security.code, self.monotonic() - started)
            return _WriteReceipt(exc.codes, exc.files, exc.rows, exc, self.monotonic() - started)
        except (RuntimeError, OSError, ValueError) as exc:
            if review_budget is None:
                for job, _window in batch:
                    self._record_failure("paired_publication", exc, job.security.code, self.monotonic() - started)
            return _WriteReceipt((), (), 0, exc, self.monotonic() - started)

    def _fill_slots(
        self,
        plans: tuple[_DownloadPlan, ...],
        futures: dict[Future[_DownloadedWindow], _DownloadJob],
        progress: QfqDownloadProgress,
    ) -> Literal["qfq_worker_unavailable"] | None:
        try:
            jobs = self._hydrate_wave(plans)
        except (RuntimeError, OSError, ValueError) as exc:
            for plan in plans:
                self._record_failure("local_read", exc, plan.security.code)
                if self.repair_supplier is not None:
                    self.v2.record_price_issue(PriceIssue(plan.security.code, plan.dates, QfqFailureCode(exc)))
                progress.record_pending(plan.security, exc, plan.dates, action="本地检查")
            return None
        for job in jobs:
            if self.cancel_requested():
                break
            future = self._submit(job)
            futures[future] = job
            if future.done() and not future.cancelled():
                error = future.exception()
                if isinstance(error, WorkerResourceRejectedError):
                    self._record_failure("worker_dispatch", error, job.security.code)
                    return "qfq_worker_unavailable"
        return None

    def _collect_completed(
        self,
        done: set[Future[_DownloadedWindow]],
        futures: dict[Future[_DownloadedWindow], _DownloadJob],
        progress: QfqDownloadProgress,
    ) -> list[tuple[_DownloadJob, _DownloadedWindow]]:
        ready: list[tuple[_DownloadJob, _DownloadedWindow]] = []
        for future in done:
            job = futures.pop(future)
            try:
                window = future.result()
            except WorkerResourceRejectedError as exc:
                self._record_failure("worker_dispatch", exc, job.security.code)
                progress.record_pending(
                    job.security, RuntimeError("qfq_worker_unavailable"), job.dates, action="任务派发"
                )
                continue
            except PriceReviewBudgetExhausted as exc:
                # No supplier outcome was established. Preserve completion,
                # price facts and the original review date for a later command.
                progress.publish("停牌复核", f"{job.security.code} | 预算耗尽 | 保留原窗口待复核", level=ProgressLevel.WARNING)
                if job.review_only:
                    progress.completed += 1
                    progress.skipped += 1
                    progress.record_latest(job.previous)
                else:
                    progress.record_pending(job.security, exc, job.dates, action="复核预算")
                continue
            except (RuntimeError, OSError, ValueError) as exc:
                if not isinstance(exc, _BaoStockCooldown):
                    self._record_failure("tencent_validation", exc, job.security.code)
                if self.repair_supplier is not None:
                    dates = job.dates
                    if isinstance(exc, HistorySupplierPendingError):
                        dates = tuple(cell.trade_date for cell in exc.window.cells if cell.status not in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended")) or dates
                    if job.baostock_basis:
                        previous_dates = {cell.trade_date for cell in self.v2.read_code(job.security.code).cells}
                        dates = tuple(sorted(set(dates) | (set(job.dates) - previous_dates)))
                    self.v2.record_price_issue(PriceIssue(job.security.code, dates, QfqFailureCode(exc)), exc.window if isinstance(exc, HistorySupplierPendingError) else None)
                progress.record_pending(job.security, exc, job.dates, action="Tencent下载")
                continue
            if all(cell.status == "tencent_inferred_suspended" for cell in window.cells):
                progress.record_no_trade_data(job.security, window.window)
            else:
                ready.append((job, window))
        return ready

    def _submit(self, job: _DownloadJob) -> Future[_DownloadedWindow]:
        if self.worker_pool is not None:
            return SubmitWithBackpressure(self.worker_pool, self._download, job)
        future: Future[_DownloadedWindow] = Future()
        try:
            future.set_result(self._download(job))
        except (RuntimeError, OSError, ValueError) as exc:
            future.set_exception(exc)
        return future

    def _accept_write(
        self,
        batch: tuple[tuple[_DownloadJob, _DownloadedWindow], ...],
        receipt: _WriteReceipt,
        progress: QfqDownloadProgress,
    ) -> tuple[str, ...]:
        progress.changed.update(receipt.files)
        progress.rows += receipt.rows
        progress.write_seconds += receipt.write_seconds
        for job, window in batch:
            if window.code in receipt.codes:
                progress.completed += 1
                self._record_completed(progress, job, window.window)
            else:
                if self.repair_supplier is not None:
                    self.v2.record_price_issue(PriceIssue(job.security.code, job.dates, QfqFailureCode(receipt.failure or RuntimeError("qfq_pair_publication_failed"))))
                progress.record_pending(
                    job.security,
                    receipt.failure or RuntimeError("qfq_pair_publication_failed"),
                    job.dates,
                    action="配对落库",
                    failure_level=ProgressLevel.ERROR,
                )
        return receipt.codes

    @staticmethod
    def _record_completed(progress: QfqDownloadProgress, job: _DownloadJob, window: PublishedHistoryWindow) -> None:
        paired = sum(cell.status == "complete" for cell in window.cells)
        progress.record_latest(window)
        progress.record_stock(
            job.security,
            QfqStockFeedback(
                "Tencent下载",
                "合格",
                paired,
                paired,
                paired,
                paired,
                inferred_suspended=sum(cell.status == "tencent_inferred_suspended" for cell in window.cells),
                reason=ProgressField(
                    f"明确停牌 {sum(cell.status == 'supplier_marked_suspended' for cell in window.cells)} 日"
                ) if any(cell.status == "supplier_marked_suspended" for cell in window.cells) else ProgressField("无"),
            ),
        )

    def _download(self, job: _DownloadJob) -> _DownloadedWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        previous_dates = {cell.trade_date for cell in job.previous.cells}
        missing = tuple(day for day in job.dates if day not in previous_dates)
        if job.baostock_basis and not job.recheck_due:
            if not self.skip_cooling_tencent_probe and not job.review_requested:
                self._fetch(job.security, job.dates[-max(5, min(len(missing) + 3, len(job.dates))):])
            raise _BaoStockCooldown()
        request_budget = self.price_review_budget if job.review_requested else None
        window: PublishedHistoryWindow | None = None
        observed_dates: tuple[date, ...] = ()
        retained_confirmed: frozenset[date] = frozenset()
        if job.previous.cells and len(missing) <= 5 and not job.baostock_basis and not job.review_requested:
            count = max(3, len(missing) + 3)
            observed_dates = job.dates[-count:]
            tail = self._fetch(job.security, observed_dates)
            tail, retained_confirmed = self._retain_confirmed_nontrading(tail, job.nontrading, job.source)
            merged = MergeDailyTail(job.previous, tail, job.dates)
            if retained_confirmed and any(cell.status == "complete" for cell in job.previous.cells) and not any(cell.status == "complete" and cell.trade_date in previous_dates for cell in tail.cells):
                # A restored nontrading fact proves coverage, not the current
                # adjustment basis of prices outside the downloaded tail.
                merged = None
            if merged is not None and tuple(cell.trade_date for cell in merged.cells) == job.dates:
                window = merged
        if window is None:
            observed_dates = job.dates
            window = self._fetch(job.security, job.dates, request_budget=request_budget)
            window, retained_confirmed = self._retain_confirmed_nontrading(window, job.nontrading, job.source)
        prior = {cell.trade_date: cell for cell in job.previous.cells}
        if any(
            cell.status in ("supplier_marked_suspended", "tencent_inferred_suspended")
            and cell.trade_date in prior
            and prior[cell.trade_date].status == "complete"
            for cell in window.cells
        ):
            raise RuntimeError("price_suspension_trading_conflict")
        if job.baostock_basis:
            reference = PublishedHistoryWindow(job.security.code, tuple(cell for cell in job.previous.cells if cell.status == "complete"))
            window = QualifyReplacementWindow(job.dates, reference, window)
        if any(cell.status not in ("complete", "supplier_marked_suspended", "tencent_inferred_suspended") for cell in window.cells):
            raise HistorySupplierPendingError(window)
        if job.observed_on is None:
            raise RuntimeError("qfq_observation_date_missing")
        checked_dates = tuple(day for day in observed_dates if day not in retained_confirmed)
        return _DownloadedWindow(window, self._observed_nontrading(window, job.source, job.observed_on, previous=job.nontrading, observed_dates=checked_dates))

    def _fetch(self, security: BaoStockSecurity, dates: tuple[date, ...], *, request_budget: PriceReviewRequestBudget | None = None) -> PublishedHistoryWindow:
        if self.cancel_requested():
            raise RuntimeError("qfq cancelled")
        window = self.supplier.fetch_window(security, dates, request_budget=request_budget) if request_budget is not None else self.supplier.fetch_window(security, dates)
        self._validate_window(security, dates, window)
        return window

    @staticmethod
    def _retain_confirmed_nontrading(
        window: PublishedHistoryWindow, previous: tuple[QfqNonTradingFact, ...], source: str,
    ) -> tuple[PublishedHistoryWindow, frozenset[date]]:
        confirmed = {fact.trade_date: fact for fact in previous if fact.kind == "confirmed" and fact.source == source}
        if not confirmed:
            return window, frozenset()
        retained = frozenset(
            cell.trade_date for cell in window.cells
            if cell.trade_date in confirmed and cell.status in ("tencent_inferred_suspended", "unknown_missing")
            and cell.unadjusted is None and cell.qfq is None
        )
        if not retained:
            return window, retained
        return replace(window, cells=tuple(confirmed[cell.trade_date].cell() if cell.trade_date in retained else cell for cell in window.cells)), retained

    @staticmethod
    def _observed_nontrading(
        window: PublishedHistoryWindow, source: str, observed_on: date, *,
        previous: tuple[QfqNonTradingFact, ...] = (), observed_dates: tuple[date, ...] | None = None,
    ) -> tuple[QfqNonTradingFact, ...]:
        prior = {fact.trade_date: fact for fact in previous}
        result: list[QfqNonTradingFact] = []
        for cell in window.cells:
            if cell.status not in ("supplier_marked_suspended", "tencent_inferred_suspended"):
                continue
            fact = prior.get(cell.trade_date)
            if observed_dates is not None and cell.trade_date not in observed_dates and fact is not None:
                result.append(fact)
            else:
                result.append(QfqNonTradingFact(window.code, cell.trade_date, "confirmed" if cell.status == "supplier_marked_suspended" else "inferred", observed_on, source))
        return tuple(result)

    @staticmethod
    def _validate_window(security: BaoStockSecurity, dates: tuple[date, ...], window: PublishedHistoryWindow) -> None:
        if window.code != security.code or tuple(cell.trade_date for cell in window.cells) != dates:
            raise RuntimeError("qfq_window_identity_or_calendar_invalid")
