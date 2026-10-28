from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.integration.test_scheduler_runtime import FixedClock, Freezes, NoopResearchRuntime, Sealing, Settlement
from tests.unit.application.test_long_runtime import _Quotes, _runtime
from tests.unit.application.test_preview_cycle import PreviewData, dependencies
from tests.unit.domain.test_preview_identity import MARKET_DATE, STARTED_AT, preview_freeze
from trader.infra.shutdown import ShutdownDeadline
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshUnavailableError,
    ReviewUnavailableError,
)
from trader.recommendation.application.runtime.cadence import PipelineTask
from trader.recommendation.application.runtime.run_scope import RecommendationRunScope
from trader.recommendation.application.runtime.scheduler_runtime import SchedulerRuntime
from trader.recommendation.domain.publication.freeze_identity import LongProjection, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy


class PreviewFreezes(Freezes):
    def build_local(self, request: CycleRequest) -> ScoredFreeze:
        return replace(
            preview_freeze(strategy=request.strategy),
            observed_at=request.observed_at,
            sequence=request.sequence,
            preview_context=request.preview_context,
        )


class RejectResearch(NoopResearchRuntime):
    def observe(self, intent, request) -> bool:
        raise AssertionError("preview must not start persistent company research")


@pytest.mark.parametrize("model_ready", (False, True))
def test_long_close_refresh_is_independent_of_short_model_preparation(application_feature_factory, model_ready):
    class ModelData(PreviewData):
        def prepare_preview(self, context):
            if not model_ready:
                raise DataRefreshUnavailableError("production_model_contract_conflict")
            super().prepare_preview(context)

    deps = dependencies(ModelData())

    def prepare_calendar():
        deps.clock.current += timedelta(seconds=5)

    scope = RecommendationRunScope(
        True, prepare_calendar, lambda: (MARKET_DATE, MARKET_DATE.replace(day=26)), deps.clock.now, lambda: "preview-a"
    )
    close = STARTED_AT.replace(day=23, hour=15)
    feature = application_feature_factory("600001", STARTED_AT)
    feature = replace(feature, quote=replace(feature.quote, source_time=close, pct_change=1.75))
    quotes = _Quotes([(feature,)])
    long_runtime, index = _runtime(quotes, completed_at=STARTED_AT)
    runtime = SchedulerRuntime(
        replace(
            deps,
            run_scope=scope,
            long_quotes=long_runtime,
            index=index,
            freezes=PreviewFreezes(),
            publish_freeze=lambda _: None,
        ),
        config_version="preview-fixture",
    )
    try:
        long_runtime.start()
        runtime.start()
        runtime.submit_due()
        assert runtime.wait_idle(3.0) and long_runtime.wait_idle(3.0)
        assert runtime.status().preview_state == ("ready" if model_ready else "failed")
        snapshot = index.snapshot(Strategy.LONG)
        assert isinstance(snapshot.current, LongProjection)
        assert snapshot.current.items[0].pct_change == 1.75
        assert snapshot.current.items[0].source_time == close
        assert snapshot.formal is None and quotes.contexts == [scope.preview]
        assert quotes.calls[0][1] == deps.clock.now()
        runtime.submit_due()
        assert long_runtime.wait_idle(3.0)
        assert len(quotes.calls) == 1
        assert not deps.sealing.calls and not deps.settlement.calls
    finally:
        assert runtime.stop(ShutdownDeadline.start(3.0)).completed
        assert long_runtime.stop(wait=True, deadline=ShutdownDeadline.start(3.0)).completed


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
        freezes=PreviewFreezes(),
        research_factory=RejectResearch,
        publish_freeze=published.append,
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
            assert isinstance(snapshot.current, ScoredFreeze)
            assert snapshot.current.stage == "hybrid"
            assert snapshot.current.trade_date == clock.now().date()
            assert snapshot.current.preview_context is scope.preview
            assert snapshot.formal is None
        assert isinstance(deps.sealing, Sealing) and not deps.sealing.calls and not deps.sealing.close_fallback_calls
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
            freezes=PreviewFreezes(),
            publish_freeze=lambda _: None,
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
