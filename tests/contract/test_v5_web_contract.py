"""V5 exposes its existing local-score scale together with engineering authority."""

from __future__ import annotations

import json
from dataclasses import replace

from tests.contract.test_web_contract import _app, _Clock, _decision, _Repository
from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.decision_events import BuildDecisionCommitted
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.domain.publication.models import RecommendationAction
from trader.web import CreateApp


def test_v5_get_and_sse_keep_local_score_and_shadow_without_reinterpreting_v2() -> None:
    _, production_queries, production_stream = _app()
    source = _decision()
    shadow = replace(
        source,
        input_versions=(
            ("score_scale", "weighted_evidence_quality_0_100"),
            ("score_model", "v5_industry_tcn:" + "a" * 64),
            ("execution_mode", "shadow"),
            ("production_authority", "false"),
        ),
        items=tuple(replace(item, action=RecommendationAction.OBSERVE) for item in source.items),
    )
    index = UnifiedDecisionIndex()
    assert index.publish(shadow, expected_version=None).accepted
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _Repository(), _Clock())
    stream = UnifiedDecisionEventStream()
    stream.publish_committed(BuildDecisionCommitted(shadow))
    status = {
        "scoring_profile": {
            "profile_id": "v5",
            "heads": {
                "tomorrow": {
                    "score_scale": "weighted_evidence_quality_0_100",
                    "production_authority": False,
                    "private_internal": "must-not-leak",
                }
            },
        }
    }
    app = CreateApp(
        services=ProfiledWebServices(
            {
                "v5": UnifiedWebServices(queries, stream, lambda: status, profile_id="v5"),
                "v2": UnifiedWebServices(production_queries, production_stream, lambda: {}, profile_id="v2"),
            },
            default_profile="v2",
            long_profile="v2",
        )
    )
    client = app.test_client()
    payload = client.get("/api/decisions/tomorrow/current?profile=v5").get_json()
    assert payload["scoring_profile"] == "v5"
    assert payload["input_versions"] == dict(shadow.input_versions)
    assert payload["items"][0]["scores"]["final"] == payload["items"][0]["scores"]["local"] == 84.0
    assert payload["items"][0]["action"] == "observe"
    head = client.get("/api/status?profile=v5").get_json()["scoring_profile"]["heads"]["tomorrow"]
    assert head == {"score_scale": "weighted_evidence_quality_0_100", "production_authority": False}
    production = client.get("/api/decisions/tomorrow/current?profile=v2").get_json()
    assert production["items"][0]["action"] == "executable"
    assert "execution_mode" not in production["input_versions"]
    assert "V5 · Shadow" in client.get("/").get_data(as_text=True)
    events = client.get("/api/events?profile=v5", headers={"Last-Event-ID": "0"}, buffered=False)
    try:
        iterator = iter(events.response)
        next(iterator)
        event = json.loads(next(iterator).decode().split("data: ", 1)[1])
    finally:
        events.close()
    assert event["input_versions"] == payload["input_versions"]
    assert event["upserts"][0]["scores"]["final_score"] == event["upserts"][0]["scores"]["local_score"] == 84.0
