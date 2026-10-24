from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from tests.integration.test_scheduler_runtime import Decisions, FixedClock, Freezes, NoopResearchRuntime, Settlement
from tests.unit.application.test_preview_cycle import PreviewData, dependencies
from tests.unit.domain.test_preview_identity import MARKET_DATE, STARTED_AT, preview_decision
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.ports.runtime import CycleRequest, ReviewUnavailableError
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.domain.publication.decision_identity import ScoredDecision
from trader.recommendation.domain.publication.models import Strategy


class PreviewDecisions(Decisions):
    def build_local(self, request: CycleRequest) -> ScoredDecision:
        return replace(
            preview_decision(strategy=request.strategy),
            observed_at=request.observed_at,
            sequence=request.sequence,
            preview_context=request.preview_context,
        )


class RejectResearch(NoopResearchRuntime):
    def observe(self, intent, request) -> bool:
        raise AssertionError("preview must not start persistent company research")


def test_saturday_after_close_runs_one_preview_without_freeze_settlement_or_archive() -> None:
    data = PreviewData()
    clock = FixedClock(STARTED_AT.replace(hour=16))
    scope = RecommendationRunScope(
        True, lambda: None, lambda: (MARKET_DATE, MARKET_DATE.replace(day=26)), clock.now, lambda: "preview-a"
    )
    published = []
    deps = replace(
        dependencies(data),
        clock=clock,
        decisions=PreviewDecisions(),
        research_factory=RejectResearch,
        publish_decision=published.append,
        run_scope=scope,
    )
    runtime = SchedulerRuntime(deps, config_version="preview-fixture")
    try:
        assert runtime.start()
        assert runtime.submit_due() == 1.0
        assert runtime.wait_idle(3.0)
        status = runtime.status()
        assert status.preview_state == "ready" and status.preview_error is None
        assert status.preview_context is scope.preview and scope.is_preview
        assert len(data.contexts) == 1
        assert [request.task for request in data.preview_requests] == [
            PipelineTask.FULL_MARKET,
            PipelineTask.INDUSTRY_HEAT,
            PipelineTask.CANDIDATE_QUOTES,
            PipelineTask.INTRADAY_TAIL,
        ]
        assert len(deps.reviews.calls) == 2
        assert set(deps.reviews.calls) == {Strategy.TOMORROW, Strategy.D25}
        for strategy in (Strategy.TOMORROW, Strategy.D25):
            snapshot = deps.index.snapshot(strategy)
            assert isinstance(snapshot.current, ScoredDecision)
            assert snapshot.current.stage == "hybrid"
            assert snapshot.current.trade_date == clock.now().date()
            assert snapshot.current.preview_context is scope.preview
            assert snapshot.formal is None
        assert isinstance(deps.freezes, Freezes) and not deps.freezes.calls and not deps.freezes.close_fallback_calls
        assert isinstance(deps.settlement, Settlement) and not deps.settlement.calls
        assert status.freeze_completed_count == status.settlement_completed_count == 0
        assert status.observer.accepted_count == status.observer.completed_count == 0
        assert len(published) == 4
        assert runtime.submit_due() == 30.0
        assert runtime.wait_idle(3.0)
        assert len(data.contexts) == 1 and len(published) == 4
    finally:
        assert runtime.stop(ShutdownDeadline.start(3.0)).completed


@pytest.mark.parametrize("fail_review", (False, True))
def test_preview_waits_for_risk_review_and_reports_failure_with_local_result(fail_review: bool) -> None:
    from tests.integration.test_scheduler_runtime import SharedReviews

    started, release = threading.Event(), threading.Event()

    class BlockingReviews(SharedReviews):
        def build_hybrid(self, local, request):
            started.set()
            assert release.wait(3.0)
            if fail_review:
                raise ReviewUnavailableError("preview_review_unavailable")
            return super().build_hybrid(local, request)

    data = PreviewData()
    deps = dependencies(data)
    scope = RecommendationRunScope(
        True, lambda: None, lambda: (MARKET_DATE, MARKET_DATE.replace(day=26)), deps.clock.now, lambda: "preview-a"
    )
    runtime = SchedulerRuntime(
        replace(
            deps,
            run_scope=scope,
            reviews=BlockingReviews(),
            decisions=PreviewDecisions(),
            publish_decision=lambda _: None,
        ),
        config_version="preview-fixture",
    )
    try:
        assert runtime.start()
        runtime.submit_due()
        assert started.wait(3.0)
        assert runtime.status().preview_state == "running"
        release.set()
        assert runtime.wait_idle(3.0)
        status = runtime.status()
        assert status.preview_state == ("failed" if fail_review else "ready")
        if fail_review:
            assert "preview_review_unavailable" in status.preview_error
            assert deps.index.snapshot(Strategy.TOMORROW).current.stage == "local"
        assert runtime.submit_due() == 30.0
        assert len(data.contexts) == 1
    finally:
        release.set()
        assert runtime.stop(ShutdownDeadline.start(3.0)).completed
