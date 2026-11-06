"""Complete-round summaries keep measurement evidence within explicit field bounds."""

from __future__ import annotations

import json

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.recommendation.web.reporting import ProjectDetails
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES

_PRIVATE = "PRIVATE_EXTERNAL_PAYLOAD"


def _operation() -> dict[str, object]:
    return {"operation": "feature_assembly", "duration_ms": 4.5, "raw": _PRIVATE}


def _stage(key: str) -> dict[str, object]:
    return {
        "key": key, "input_count": 8, "output_count": 6, "rejected_count": 1,
        "pending_count": 1, "failed_count": 0, "not_selected_count": 0, "duration_ms": None,
        "operation_durations": [_operation()], "raw": _PRIVATE,
    }


def _head(profile: str, strategy: str) -> dict[str, object]:
    return {
        "profile": profile, "strategy": strategy, "publication_id": "freeze:" + profile + ":" + strategy,
        "projection_version": "projection:" + profile + ":" + strategy,
        "observed_at": "2026-11-06T14:00:00+08:00", "trade_date": "2026-11-06",
        "api_received_at": "2026-11-06T14:00:04+08:00", "selected_count": 6, "evaluated_count": 8,
        "quote_source_min": "2026-11-06T13:59:59+08:00", "quote_source_max": "2026-11-06T14:00:00+08:00",
        "decision_source_age_max_seconds": 1.0, "event_delivery_source_age_max_seconds": 3.0,
        "api_delivery_source_age_max_seconds": 5.0,
        "stages": [_stage(stage.value) for stage in PIPELINE_STAGES],
        "prices": [123.45], "raw": _PRIVATE,
    }


def _round() -> dict[str, object]:
    return {
        "round_sequence": 17, "event_id": 92, "session_generation": 3,
        "event_received_at": "2026-11-06T14:00:02+08:00", "outcome": "source_age_qualified", "reason": None,
        "scheduler_elapsed_ms": 7000.0,
        "heads": [_head(profile, strategy) for profile in ("v1", "v2", "v3", "v5") for strategy in ("tomorrow", "d25")],
        "source_private": _PRIVATE,
    }


def _report() -> dict[str, object]:
    return {
        "mode": "complete_rounds",
        "summary": {
            "required_complete_rounds": 5, "observed_batch_events": 7, "complete_published_rounds": 6,
            "current_consecutive_qualified_rounds": 5, "maximum_consecutive_qualified_rounds": 5,
            "round_target_policy": "consecutive_published_sequence_and_qualified_atomic_heads",
            "identity_matched_rounds": 5, "decision_source_age_qualified_rounds": 5,
            "delivery_source_age_qualified_rounds": 5, "source_age_qualified_rounds": 5,
            "duplicate_rounds": 1, "duplicate_events": 0, "initial_replay_events": 1,
            "http_poll_samples": 8, "stop_reason": "goal_reached", "scope": "live_sse_atomic_api",
            "performance_acceptance": False, "browser_acceptance": False, "freeze_recovery_acceptance": False,
            "raw": _PRIVATE,
        },
        "configuration": {
            "observation_seconds": 900.0, "minimum_poll_interval_seconds": 15.0, "status_profile": "v2",
            "quote_age_limit_seconds": 30.0, "required_age_scope": "decision_and_delivery",
            "maximum_round_records": 64, "maximum_stream_bytes": 33_554_432, "source_private": _PRIVATE,
        },
        "runtime_identity": {
            "runtime_version": "runtime:frozen", "session_generation": 3, "initial_event_cursor": 80,
            "last_event_cursor": 92, "raw": _PRIVATE,
        },
        "reasons": [{"code": "superseded", "count": 1, "raw": _PRIVATE}],
        "rounds": [_round()],
        "raw": _PRIVATE,
    }


def _project(source: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    ProjectDetails(DiagnosticResult("web_health", 0, 10.0, source, None), source, payload)
    return payload


def test_complete_round_projection_preserves_eight_heads_and_their_bound_eleven_stages():
    source = _report()

    payload = _project(source)

    assert payload["mode"] == "complete_rounds"
    assert payload["summary"] == {key: value for key, value in source["summary"].items() if key != "raw"}
    assert payload["configuration"] == {
        key: value for key, value in source["configuration"].items() if key != "source_private"
    }
    assert payload["runtime_identity"] == {
        key: value for key, value in source["runtime_identity"].items() if key != "raw"
    }
    round_, = payload["rounds"]
    assert round_["round_sequence"] == 17 and round_["event_id"] == 92
    assert round_["session_generation"] == 3 and round_["scheduler_elapsed_ms"] == 7000.0
    assert {(head["profile"], head["strategy"]) for head in round_["heads"]} == {
        (profile, strategy) for profile in ("v1", "v2", "v3", "v5") for strategy in ("tomorrow", "d25")
    }
    for head in round_["heads"]:
        assert head["publication_id"] and head["projection_version"]
        assert head["observed_at"] == "2026-11-06T14:00:00+08:00"
        assert head["selected_count"] == 6 and head["evaluated_count"] == 8
        assert head["decision_source_age_max_seconds"] == 1.0
        assert head["event_delivery_source_age_max_seconds"] == 3.0
        assert head["api_delivery_source_age_max_seconds"] == 5.0
        assert tuple(stage["key"] for stage in head["stages"]) == tuple(stage.value for stage in PIPELINE_STAGES)
        for stage in head["stages"]:
            assert stage["duration_ms"] is None
            assert stage["input_count"] == 8 and stage["output_count"] == 6
            assert stage["operation_durations"] == [{"operation": "feature_assembly", "duration_ms": 4.5}]
    assert payload["reasons"] == [{"code": "superseded", "count": 1}]
    rendered = json.dumps(payload)
    assert _PRIVATE not in rendered and "123.45" not in rendered
    assert "prices" not in rendered and "raw" not in rendered and "source_private" not in rendered


@pytest.mark.parametrize("boundary", ("rounds", "heads", "stages", "operation_durations", "reasons"))
def test_complete_round_projection_caps_each_collection_without_multiplying_large_fixtures(boundary):
    source = {"mode": "complete_rounds", "rounds": [{"heads": [{"stages": [{"operation_durations": []}]}]}]}
    if boundary == "rounds":
        source["rounds"] = [{"heads": []} for _ in range(65)]
    elif boundary == "heads":
        source["rounds"][0]["heads"] = [{"stages": []} for _ in range(9)]
    elif boundary == "stages":
        source["rounds"][0]["heads"][0]["stages"] = [_stage("local_data") for _ in range(12)]
    elif boundary == "operation_durations":
        source["rounds"][0]["heads"][0]["stages"][0]["operation_durations"] = [_operation() for _ in range(17)]
    else:
        source["reasons"] = [{"code": "superseded", "count": 1} for _ in range(33)]

    payload = _project(source)

    if boundary == "rounds":
        assert len(payload["rounds"]) == 64
    elif boundary == "heads":
        assert len(payload["rounds"][0]["heads"]) == 8
    elif boundary == "stages":
        assert len(payload["rounds"][0]["heads"][0]["stages"]) == 11
    elif boundary == "operation_durations":
        assert len(payload["rounds"][0]["heads"][0]["stages"][0]["operation_durations"]) == 16
    else:
        assert len(payload["reasons"]) == 32


def test_complete_round_projection_does_not_reuse_single_profile_sample_layout():
    source = _report()
    source["samples"] = [{"raw": _PRIVATE}]

    payload = _project(source)

    assert "publication_samples" not in payload
    assert set(payload["latest_runtime"]) == {"phase", "round_sequence", "round_outcome"}
    assert "samples" not in payload and _PRIVATE not in json.dumps(payload)
