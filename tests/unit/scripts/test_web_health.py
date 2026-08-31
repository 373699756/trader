from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from scripts.runtime_diagnostics.web_health import WebSample, analyze_samples, build_report, parse_web_sample

_STRATEGY = "tomorrow"
_TRADE_DATE = "2026-08-25"


def _funnel(**overrides: int) -> dict[str, int]:
    values = {
        "requested_candidates": 360,
        "candidate_features": 360,
        "security_master": 360,
        "history": 360,
        "filter_pass": 65,
        "filter_observe": 0,
        "filter_reject": 295,
        "full_scored": 65,
        "review_eligible": 20,
        "action_executable": 0,
        "action_observe": 0,
        "action_unavailable": 65,
        "selected_executable": 0,
        "selected_observe": 0,
    }
    values.update(overrides)
    return values


def _sample(
    number: int,
    *,
    funnel: Mapping[str, int] | None = None,
    decision_status: str = "ready",
    quality_status: str | None = None,
    empty_reason: str | None = "risk_or_execution_blocked",
    projection_version: str = "projection-1",
    event_sequence: int | None = None,
    phase: str = "today_main",
    items: list[dict[str, str]] | None = None,
    include_quality_trade_date: bool = True,
    warmup: tuple[int, int, int, int] = (20, 15, 0, 5),
    warmup_timeout_count: int = 0,
) -> WebSample:
    quality = (
        {
            "status": quality_status or decision_status,
            "publishable": (quality_status or decision_status) in {"ready", "business_empty"},
            "primary_blocker": "ready" if decision_status == "ready" else "history_coverage_incomplete",
            "history_required_sessions": 61,
            "population_filter_reason_counts": {"stale_quote": 5571},
            "candidate_filter_reason_counts": {"history_too_short": 71},
            "candidate_transient_reason_counts": {"history_data_degraded": 12},
            "candidate_optional_reason_counts": {"research_data_degraded": 3},
            "supply_reason_counts": {"local_score_unavailable": 17},
            "supply_funnel": dict(funnel or _funnel()),
            "summary": {
                **({"trade_date": _TRADE_DATE} if include_quality_trade_date else {}),
                "quote_total_count": 360,
                "quote_covered_count": 360,
                "quote_missing_count": 0,
                "security_identity_missing_count": 0,
                "highest_final_score": 74.0,
            },
        }
        if funnel is not None or decision_status != "not_ready"
        else None
    )
    status: dict[str, object] = {
        "schema_version": "v2_status_v9",
        "release": {"decision_view_schema": "v2_decision_view_v3", "web_asset_revision": "test"},
        "status": "running",
        "runtime_started": True,
        "runtime_version": "runtime-test",
        "phase": phase,
        "market_data": {
            "market_feature_rows": 5500,
            "candidate_quote_cache_entries": 360,
            "history_universe_rows": 360,
            "history_covered_rows": warmup[1],
            "history_coverage_ratio": warmup[1] / 360,
            "history_warmup_planned_count": warmup[0],
            "history_warmup_completed_count": warmup[1],
            "history_warmup_failure_count": warmup[2],
            "history_warmup_inflight_count": warmup[3],
            "history_warmup_retry_deferred_count": warmup[2],
            "history_warmup_unique_failure_count": warmup[2],
            "history_warmup_timeout_count": warmup_timeout_count,
            "history_warmup_inflight_age_seconds": 1.5 if warmup[3] else None,
            "history_warmup_batch_timeout_seconds": 20.0,
            "history_warmup_last_source": "tencent",
        },
        "scheduler": {"input_quality": {_STRATEGY: quality} if quality is not None else {}},
        "strategies": {
            _STRATEGY: {
                "status": decision_status,
                "trade_date": _TRADE_DATE,
                "projection_version": projection_version,
                "coverage": {
                    "candidate_count": 360 if decision_status == "ready" else 0,
                    "evaluated_count": 65 if decision_status == "ready" else 0,
                    "rejected_count": 295 if decision_status == "ready" else 0,
                    "selected_count": 0,
                },
            }
        },
        "events": {"sequence": event_sequence if event_sequence is not None else number},
    }
    decision: dict[str, object] = {
        "schema_version": "v2_decision_view_v3",
        "status": decision_status,
        "strategy": _STRATEGY,
        "trade_date": _TRADE_DATE,
        "projection_version": projection_version,
        "coverage": {
            "candidate_count": 360 if decision_status == "ready" else 0,
            "evaluated_count": 65 if decision_status == "ready" else 0,
            "rejected_count": 295 if decision_status == "ready" else 0,
            "selected_count": 0,
        },
        "selection_diagnostics": {"empty_reason": empty_reason} if empty_reason is not None else None,
        "items": items or [],
    }
    return parse_web_sample(
        number,
        f"2026-08-25T10:00:0{number}+08:00",
        status_payload=status,
        decision_payloads={_STRATEGY: decision},
    )


def test_legal_zero_recommendations_with_complete_funnel_are_not_reported_as_anomaly() -> None:
    findings = analyze_samples(
        tuple(_sample(number, funnel=_funnel()) for number in range(1, 4)),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert findings == ()


def test_business_empty_zero_scored_stage_is_not_reported_as_pipeline_stall() -> None:
    all_filtered = _funnel(
        filter_pass=0,
        filter_reject=360,
        full_scored=0,
        action_unavailable=0,
    )
    samples = tuple(_sample(number, funnel=all_filtered, quality_status="business_empty") for number in range(1, 4))

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert not any(finding.code == "funnel_full_scored_persistently_zero" for finding in findings)


def test_persistent_zero_scoring_with_populated_quote_cache_is_reported() -> None:
    stalled = _funnel(
        security_master=0,
        history=0,
        filter_pass=0,
        filter_reject=0,
        full_scored=0,
        review_eligible=0,
        action_unavailable=0,
    )
    samples = tuple(
        _sample(number, funnel=stalled, decision_status="not_ready", empty_reason=None) for number in range(1, 4)
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)
    codes = {finding.code for finding in findings}

    assert "funnel_full_scored_persistently_zero" in codes
    assert "funnel_security_master_persistently_zero" in codes
    assert "funnel_history_persistently_zero" in codes
    assert "selected_executable_persistently_zero" not in codes


def test_nonzero_funnel_stage_regressing_to_zero_is_reported_immediately() -> None:
    samples = (
        _sample(1, funnel=_funnel(full_scored=65), event_sequence=10),
        _sample(2, funnel=_funnel(full_scored=0), event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(
        finding.code == "funnel_regressed_to_zero"
        and finding.strategy == _STRATEGY
        and finding.evidence.get("field") == "full_scored"
        for finding in findings
    )


def test_legacy_global_history_gate_blocking_existing_scores_is_reported() -> None:
    sample = _sample(
        1,
        funnel=_funnel(history=73, full_scored=46),
        decision_status="not_ready",
        quality_status="not_ready",
        empty_reason=None,
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(
        finding.code == "global_history_gate_blocked_eligible_candidates"
        and finding.evidence.get("full_scored") == 46
        and finding.evidence.get("history") == 73
        for finding in findings
    )


def test_input_quality_disappearing_during_scoring_is_reported() -> None:
    samples = (
        _sample(1, funnel=_funnel(), event_sequence=10),
        _sample(2, funnel=None, decision_status="not_ready", empty_reason=None, event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert any(finding.code == "input_quality_disappeared" for finding in findings)


def test_status_and_current_projection_mismatch_is_reported() -> None:
    sample = _sample(1)
    decision = replace(sample.decisions[_STRATEGY], projection_version="different-projection")
    mismatched = replace(sample, decisions={_STRATEGY: decision})

    findings = analyze_samples((mismatched,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(
        finding.code == "status_current_projection_mismatch" and finding.severity == "warning" for finding in findings
    )


def test_status_and_current_coverage_mismatch_is_reported() -> None:
    sample = _sample(1)
    decision = sample.decisions[_STRATEGY]
    mismatched_decision = replace(
        decision,
        coverage=replace(decision.coverage, selected_count=1),
        item_count=1,
    )
    mismatched = replace(sample, decisions={_STRATEGY: mismatched_decision})

    findings = analyze_samples((mismatched,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(finding.code == "status_current_selected_count_mismatch" for finding in findings)


def test_ready_zero_selection_without_empty_reason_is_reported() -> None:
    findings = analyze_samples(
        (_sample(1, funnel=_funnel(), empty_reason=None),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(finding.code == "legal_empty_diagnostics_missing" for finding in findings)


def test_input_quality_without_trade_date_is_reported_as_invalid_shape() -> None:
    findings = analyze_samples(
        (_sample(1, funnel=_funnel(), include_quality_trade_date=False),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert any(finding.code == "input_quality_shape_invalid" for finding in findings)


def test_scoring_regression_is_ignored_after_strategy_freezes() -> None:
    samples = (
        _sample(1, funnel=_funnel(full_scored=65), event_sequence=10),
        _sample(2, funnel=_funnel(full_scored=0), event_sequence=11, phase="frozen"),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert not any(finding.code == "funnel_regressed_to_zero" for finding in findings)
    assert not any(finding.code.endswith("persistently_zero") for finding in findings)


def test_runtime_restart_splits_persistent_zero_window() -> None:
    stalled = _funnel(security_master=0, history=0, full_scored=0)
    samples = (
        _sample(1, funnel=stalled, decision_status="not_ready", empty_reason=None, event_sequence=9),
        _sample(2, funnel=stalled, decision_status="not_ready", empty_reason=None, event_sequence=10),
        _sample(3, funnel=stalled, decision_status="not_ready", empty_reason=None, event_sequence=1),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(finding.code == "runtime_restart_observed" for finding in findings)
    assert not any(finding.code.endswith("persistently_zero") for finding in findings)


def test_history_warmup_failure_growth_is_reported_with_counter_delta() -> None:
    samples = (
        _sample(1, warmup=(100, 75, 20, 5), event_sequence=10),
        _sample(2, warmup=(105, 75, 25, 5), event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert any(
        finding.code == "history_warmup_failures_increased"
        and finding.severity == "warning"
        and finding.evidence.get("previous") == 20
        and finding.evidence.get("current") == 25
        for finding in findings
    )


def test_history_warmup_timeout_growth_is_reported_as_error() -> None:
    samples = (
        _sample(1, warmup_timeout_count=2, event_sequence=10),
        _sample(2, warmup_timeout_count=3, event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert any(
        finding.code == "history_warmup_timeouts_increased"
        and finding.severity == "error"
        and finding.evidence.get("delta") == 1
        for finding in findings
    )


def test_json_report_contains_only_aggregated_projection_data() -> None:
    sample = _sample(1, items=[{"code": "600000", "name": "sensitive"}])
    report = build_report(
        "http://127.0.0.1:5000",
        (sample,),
        (),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    rendered = str(report)
    assert "600000" not in rendered
    assert "items" not in rendered
    assert report["schema_version"] == "web_recommendation_health_v3"
    assert report["status"] == "passed"
    assert report["samples"][0]["market"]["history_warmup"]["planned_count"] == 20
    assert report["samples"][0]["market"]["history_warmup"]["batch_timeout_seconds"] == 20.0
    strategy = report["samples"][0]["strategies"][_STRATEGY]
    assert strategy["history_required_sessions"] == 61
    assert strategy["supply_funnel"] == _funnel()
    assert strategy["population_filter_reason_counts"] == {"stale_quote": 5571}
    assert strategy["candidate_filter_reason_counts"] == {"history_too_short": 71}
    assert strategy["candidate_transient_reason_counts"] == {"history_data_degraded": 12}
    assert strategy["candidate_optional_reason_counts"] == {"research_data_degraded": 3}
    assert strategy["supply_reason_counts"] == {"local_score_unavailable": 17}
