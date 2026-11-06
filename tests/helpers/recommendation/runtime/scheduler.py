from __future__ import annotations
import threading
from dataclasses import replace
from datetime import date, datetime
from tests.helpers.recommendation.publication.freeze_identity import NOW, freeze
from trader.infra.shutdown import ShutdownStep
from trader.infra.workers import BoundedExecutor
from trader.recommendation.application.pipeline.freeze_publish.freeze_observers import AsyncFreezeObserver
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.runtime import CycleRequest, RefreshOutcome, ResearchIntent, ResearchRuntimeStatus, SharedDeepSeekRuntimeContract
from trader.recommendation.application.runtime.cadence import CadencePlanner, CadencePolicy, PipelineTask
from trader.recommendation.application.runtime.runtime_dependencies import RuntimeDependencies
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FreezeOverlay,
    LongProjection,
    LongProjectionItem,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy


class TradingCalendar:
    def is_trading_day(self, _day: date) -> bool:
        return True


class FixedClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.current.timestamp()


class DataRefresh:
    def __init__(self) -> None:
        self.calls: list[Strategy] = []
        self.requests: list[CycleRequest] = []
        self.task_requests: list[object] = []
        self.released_requests: list[CycleRequest] = []

    def refresh(self, request: CycleRequest) -> None:
        self.calls.append(request.strategy)
        self.requests.append(request)

    def scoring_epoch(self) -> str:
        return "test:shared_input"

    def refresh_pair(self, requests: tuple[CycleRequest, CycleRequest]) -> None:
        for request in requests:
            self.refresh(request)

    def release_scoring_input(self, request: CycleRequest) -> None:
        self.released_requests.append(request)

    def refresh_task(self, request) -> RefreshOutcome:
        self.task_requests.append(request)
        return RefreshOutcome(
            request.task,
            True,
            f"test:{request.task.value}:{request.observed_at:%H%M%S%f}",
            (),
            request.observed_at,
            False,
        )


class BlockingScoringInputs(DataRefresh):
    def __init__(self, *, block_full_market: bool = False) -> None:
        super().__init__()
        self.full_market_started = threading.Event()
        self.release_full_market = threading.Event()
        if not block_full_market:
            self.release_full_market.set()
        self.candidate_started = threading.Event()
        self.release_candidate = threading.Event()

    def refresh_task(self, request) -> RefreshOutcome:
        outcome = super().refresh_task(request)
        if request.task is PipelineTask.FULL_MARKET:
            self.full_market_started.set()
            assert self.release_full_market.wait(2.0)
        elif request.task is PipelineTask.CANDIDATE_QUOTES:
            self.candidate_started.set()
            assert self.release_candidate.wait(2.0)
        return outcome


def _cadence(at: datetime) -> CadencePlanner:
    return CadencePlanner(
        CadencePolicy.from_seconds(
            {
                "full_market": {"before_close": 10},
                "candidate_quotes": {"before_close": 10},
                "topk_quotes": {"before_close": 10, "after_close": 10},
                "long_quotes": {"before_close": 10, "after_close": 10},
                "score": {"before_close": 10},
                "intraday_tail": {"afternoon": 10, "final_review": 10},
                "industry_heat": {"warmup": 120, "morning": 60, "afternoon": 60, "final_review": 60},
            }
        ),
        started_at=at,
    )


class Freezes:
    def input_quality_status(self):
        return ()

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        del strategy, trade_date
        return False

    def build_local(self, request: CycleRequest):
        if request.strategy is Strategy.LONG:
            return LongProjection(
                request.trade_date,
                request.sequence,
                request.observed_at,
                (("quotes", request.input_version),),
                (LongProjectionItem("600001", "core", request.input_version),),
            )
        current = freeze(request.strategy, sequence=request.sequence)
        items = tuple(
            replace(
                item,
                quote=replace(item.quote, source_time=request.observed_at) if item.quote is not None else None,
            )
            for item in current.items
        )
        return replace(
            current,
            trade_date=request.trade_date,
            observed_at=request.observed_at,
            items=items,
        )

    def initial_overlay(self, freeze: ScoredFreeze) -> FreezeOverlay:
        quotes = tuple(item.quote for item in freeze.items if item.selected and item.quote is not None)
        return FreezeOverlay(freeze.strategy, freeze.trade_date, freeze.version, freeze.observed_at, quotes, sequence=1)

    def refreshed_overlay(
        self,
        freeze: ScoredFreeze,
        request: CycleRequest,
        previous: FreezeOverlay | None,
    ) -> FreezeOverlay | None:
        del previous
        quotes = tuple(
            replace(
                item.quote,
                price=(item.quote.price or 0.0) + 1.0,
                source_time=request.observed_at,
                data_version=request.input_version,
            )
            for item in freeze.items
            if item.selected and item.quote is not None
        )
        if not quotes:
            return None
        return FreezeOverlay(
            freeze.strategy, freeze.trade_date, freeze.version, request.observed_at, quotes, sequence=1
        )

    def research_audit_factory(self, version: str):
        del version
        return lambda: None

    def tomorrow_profile_research_input(self, version: str):
        del version
        return None

    def research_intent(self, current: ScoredFreeze) -> ResearchIntent:
        codes = tuple(item.code for item in current.items)
        return ResearchIntent(current.strategy, current.trade_date, codes, codes)


class NoopResearchRuntime:
    def start(self) -> bool:
        return True

    def stop(self, *, wait: bool, deadline=None) -> ShutdownStep:
        del wait, deadline
        return ShutdownStep("research", completed=True, timed_out=False)

    def observe(self, intent, request) -> bool:
        del intent, request
        return False

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def status(self):
        return ResearchRuntimeStatus(state="idle")


def noop_research_factory():
    return NoopResearchRuntime()


class SharedReviews:
    runtime_contract = SharedDeepSeekRuntimeContract(
        daily_physical_limit=168,
        shared_cache=True,
        shared_single_flight=True,
    )

    def __init__(self) -> None:
        self.calls: list[Strategy] = []

    def build_hybrid(self, local: ScoredFreeze, request: CycleRequest) -> ScoredFreeze:
        self.calls.append(request.strategy)
        return replace(
            local,
            sequence=local.sequence + 1,
            stage="hybrid",
            parent_version=local.version,
            items=(replace(local.items[0], final_score=89.0),),
        )


def _round_cleanup_runtime() -> SchedulerRuntime:
    return SchedulerRuntime(
        RuntimeDependencies(
            control_pool=BoundedExecutor(worker_count=1, queue_capacity=4, thread_name_prefix="test-round-cleanup"),
            clock=FixedClock(NOW), calendar=TradingCalendar(), cadence=_cadence(NOW),
            data=DataRefresh(), freezes=Freezes(), reviews=SharedReviews(), index=UnifiedFreezeIndex(),
            observer=AsyncFreezeObserver((), capacity=4, thread_name="test-round-cleanup-observer"),
            sealing=Sealing(), settlement=Settlement(), research_factory=noop_research_factory,
            publish_freeze=lambda _event: None, publish_overlay=lambda _overlay: None,
        ),
        config_version="runtime-current",
        shutdown_timeout_seconds=2.0,
    )


class Sealing:
    def __init__(self, index: UnifiedFreezeIndex | None = None) -> None:
        self._index = index
        self.calls: list[tuple[Strategy, str | None]] = []
        self.close_fallback_calls: list[tuple[Strategy, str, str]] = []

    def freeze(self, strategy: Strategy, at: datetime, current) -> None:
        self.calls.append((strategy, current.version if current is not None else None))
        if self._index is None:
            return
        sealed = self._index.seal_for_freeze(strategy, boundary_at=at)
        assert sealed.accepted and sealed.freeze is not None
        assert self._index.commit_formal(CommittedFreezeRecord(sealed.freeze, at, "scheduled"))

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        at: datetime,
        current,
        *,
        recovery_path: str,
        official_close_version: str,
    ) -> None:
        self.close_fallback_calls.append((strategy, recovery_path, official_close_version))
        if self._index is not None:
            sealed = self._index.seal_close_fallback(
                current, boundary_at=at, official_close_version=official_close_version
            )
            assert sealed.accepted and sealed.freeze is not None
            assert self._index.commit_formal(CommittedFreezeRecord(sealed.freeze, at, "close_fallback"))


class Settlement:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def settle(self, at: datetime) -> None:
        self.calls.append(at)


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = threading.Event()
    for _attempt in range(int(timeout / 0.01)):
        if predicate():
            return True
        deadline.wait(0.01)
    return predicate()
