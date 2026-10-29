"""V5 exposes its model-prediction rank and production identity."""

from __future__ import annotations

import json
from dataclasses import replace

from tests.contract.test_web_contract import _app, _Clock, _freeze, _Repository
from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import BuildFreezeCommitted
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.publication.models import RecommendationAction
from trader.web import CreateApp


def test_v5_get_and_sse_expose_model_rank_without_reinterpreting_v2() -> None:
    _, production_queries, production_stream = _app()
    source = _freeze()
    ranked = replace(
        source,
        input_versions=(
            ("score_scale", "model_prediction_percentile_0_100"),
            ("score_model_id", "v5_industry_tcn"),
            ("score_model", "v5_industry_tcn:" + "a" * 64),
            ("score_contract_hash", "b" * 64),
            ("rank_population", "model-prediction-population:" + "c" * 64),
            ("execution_mode", "production"),
            ("production_authority", "true"),
        ),
        items=tuple(
            replace(item, action=RecommendationAction.EXECUTABLE, final_score=99.0) for item in source.items
        ),
    )
    index = UnifiedFreezeIndex()
    assert index.publish(ranked, expected_version=None).accepted
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), _Repository(), _Clock())
    stream = UnifiedFreezeEventStream()
    stream.publish_committed(BuildFreezeCommitted(ranked))
    status = {
        "scoring_profile": {
            "profile_id": "v5",
            "heads": {
                "tomorrow": {
                    "score_scale": "model_prediction_percentile_0_100",
                    "production_authority": True,
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
    payload = client.get("/api/freezes/tomorrow/current?profile=v5").get_json()
    assert payload["scoring_profile"] == "v5"
    assert payload["input_versions"] == dict(ranked.input_versions)
    assert payload["items"][0]["scores"]["local"] == 84.0
    assert payload["items"][0]["scores"]["final"] == 99.0
    assert payload["items"][0]["action"] == "executable"
    head = client.get("/api/status?profile=v5").get_json()["scoring_profile"]["heads"]["tomorrow"]
    assert head == {"score_scale": "model_prediction_percentile_0_100", "production_authority": True}
    production = client.get("/api/freezes/tomorrow/current?profile=v2").get_json()
    assert production["items"][0]["action"] == "executable"
    assert "execution_mode" not in production["input_versions"]
    assert "V5 · Shadow" not in client.get("/").get_data(as_text=True)
    events = client.get("/api/events?profile=v5", headers={"Last-Event-ID": "0"}, buffered=False)
    try:
        iterator = iter(events.response)
        next(iterator)
        event = json.loads(next(iterator).decode().split("data: ", 1)[1])
    finally:
        events.close()
    assert event["input_versions"] == payload["input_versions"]
    assert event["upserts"][0]["scores"]["local_score"] == 84.0
    assert event["upserts"][0]["scores"]["final_score"] == 99.0
