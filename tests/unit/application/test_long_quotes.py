from dataclasses import replace
from datetime import date, timedelta

from trader.application.long_groups import LongGroupDefinition, LongWatchItemDefinition
from trader.application.long_quotes import LongProjectionRequest, LongQuoteProjectionService
from trader.application.snapshot_workflow import _active_overlay_targets, _active_v2_overlay_targets
from trader.application.status import RuntimeState
from trader.domain.recommendation.models import RecommendationAction, Strategy


def test_projection_keeps_fixed_order_and_marks_scoring_not_applicable(
    application_feature_factory,
    utc_now,
) -> None:
    first = application_feature_factory("600001", utc_now, industry="设备")
    first = replace(first, quote=replace(first.quote, name="甲公司"))
    service = LongQuoteProjectionService(
        codes=("600001", "600002"),
        items=(
            LongWatchItemDefinition("600001", "甲公司", "设备"),
            LongWatchItemDefinition("600002", "乙公司", "材料"),
        ),
        groups=(LongGroupDefinition("观察组", "chokepoint", ("600001", "600002")),),
    )

    snapshot = service.project(
        (first,),
        LongProjectionRequest(None, utc_now, "today_main", "2026-07-16", "long-quotes-v1", "config-v2"),
    )

    assert snapshot.strategy is Strategy.LONG
    assert [item.features.quote.code for item in snapshot.recommendations] == ["600001", "600002"]
    assert snapshot.metadata["score_status"] == "not_applicable"
    assert snapshot.metadata["quote_covered_count"] == 1
    assert snapshot.degraded_reasons == ("long_quotes_partial",)
    assert all(item.action is RecommendationAction.OBSERVE for item in snapshot.recommendations)
    assert all(item.score.final_score == 0.0 for item in snapshot.recommendations)
    assert snapshot.recommendations[0].target_price is None
    assert snapshot.recommendations[0].features.values == {}
    assert snapshot.recommendations[1].features.quote.price is None


def test_projection_reuses_same_day_quote_for_partial_refresh(
    application_feature_factory,
    utc_now,
) -> None:
    service = LongQuoteProjectionService(codes=("600001", "600002"))
    first = service.project(
        (
            application_feature_factory("600001", utc_now),
            application_feature_factory("600002", utc_now),
        ),
        LongProjectionRequest(None, utc_now, "today_main", "2026-07-16", "long-quotes-v1", "config-v2"),
    )

    second = service.project(
        (application_feature_factory("600001", utc_now + timedelta(seconds=1)),),
        LongProjectionRequest(
            first,
            utc_now + timedelta(seconds=1),
            "today_main",
            "2026-07-16",
            "long-quotes-v2",
            "config-v2",
        ),
    )

    retained = second.recommendations[1].features.quote
    assert retained.code == "600002"
    assert retained.source_time == utc_now
    assert second.metadata["quote_covered_count"] == 1
    assert second.metadata["quote_retained_count"] == 1


def test_shared_topk_overlay_excludes_long_snapshot(application_feature_factory, utc_now) -> None:
    state = RuntimeState()
    snapshot = LongQuoteProjectionService(codes=("600001",)).project(
        (application_feature_factory("600001", utc_now),),
        LongProjectionRequest(None, utc_now, "today_main", "2026-07-16", "long-quotes-v1", "config-v2"),
    )
    state.publish(snapshot)
    pipeline = type(
        "_Pipeline",
        (),
        {
            "_state": state,
            "_live_overlays": {},
            "_repository": type("_Repository", (), {"load_live_overlay": staticmethod(lambda *_args: None)})(),
        },
    )()

    assert _active_overlay_targets(pipeline, "2026-07-16") == ()


def test_v2_overlay_target_failure_is_degraded_without_blocking_other_targets() -> None:
    state = RuntimeState()

    class FailedSink:
        @staticmethod
        def overlay_codes(_trade_date):
            raise RuntimeError("injected")

    class HealthySink:
        @staticmethod
        def overlay_codes(_trade_date):
            return ("600001",)

    healthy = HealthySink()
    pipeline = type("_Pipeline", (), {"_state": state, "_v2_overlays": (FailedSink(), healthy)})()

    assert _active_v2_overlay_targets(pipeline, date(2026, 8, 11)) == ((healthy, ("600001",)),)
    assert state.snapshot()["last_error"] == "V2 live overlay degraded: RuntimeError"
