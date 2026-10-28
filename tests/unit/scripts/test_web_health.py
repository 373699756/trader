from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from scripts.runtime_diagnostics.web_health import (
    WebSample,
    analyze_samples,
    build_report,
    collect_samples,
    parse_web_sample,
)
from trader.recommendation.domain.evidence.pipeline import PIPELINE_STAGES

_STRATEGY = "tomorrow"
_TRADE_DATE = "2026-08-25"


@pytest.mark.parametrize("ready", (False, True))
def test_runtime_collects_long_without_applying_short_strategy_scoring_rules(monkeypatch, ready):
    requests = []
    long_payload = {
        "strategy": "long",
        "status": "ready" if ready else "not_ready",
        "coverage": {"item_count": int(ready), "available_quote_count": int(ready)},
        "items": [{"code": "private-code", "quote": {"price": 123.45}}] if ready else [],
    }

    def fetch(url, timeout_seconds):
        requests.append(url)
        return long_payload if url.endswith("/long/current") else {}

    monkeypatch.setattr("scripts.runtime_diagnostics.web_health._fetch_mapping", fetch)
    samples = collect_samples(
        "http://127.0.0.1:5000", strategies=(_STRATEGY,), sample_count=1, interval_seconds=0, timeout_seconds=1
    )
    findings = analyze_samples(samples, strategies=(_STRATEGY,))
    assert any(item.code == "long_projection_unavailable" for item in findings) is not ready
    assert not any(item.strategy == "long" and item.code != "long_projection_unavailable" for item in findings)
    assert requests[-1].endswith("/long/current")
    report = build_report(
        "http://127.0.0.1:5000", samples, findings, strategies=(_STRATEGY,), consecutive_zero_threshold=1
    )
    summary = report["samples"][0]["strategies"]["long"]
    assert summary["current_item_count"] == int(ready)
    assert summary["available_quote_count"] == int(ready)
    assert "private-code" not in str(report) and "123.45" not in str(report)


def _funnel(**overrides: int) -> dict[str, object]:
    values = {
        "issuer_eligible_population": 5500,
        "dynamic_filter_eligible": 420,
        "strategy_history_eligible": 360,
        "model_input_eligible": 360,
        "scoring_input_eligible": 360,
        "scoring_input_count": 360,
        "requested_candidates": 360,
        "candidate_features": 360,
        "candidate_quote_eligible": 360,
        "security_master": 360,
        "history": 360,
        "filter_pass": 65,
        "filter_observe": 0,
        "filter_reject": 295,
        "full_scored": 65,
        "review_eligible": 20,
        "observation_threshold_met_count": 0,
        "executable_threshold_met_count": 0,
        "action_executable": 0,
        "action_observe": 0,
        "action_unavailable": 65,
        "selected_executable": 0,
        "selected_observe": 0,
    }
    values.update(overrides)
    values.setdefault("input_ready_population", values["issuer_eligible_population"])
    seats = min(6, values["full_scored"])
    return {
        "current_stage": "action_publish",
        "stages": [
            _stage("local_data", values["issuer_eligible_population"], values["input_ready_population"]),
            _stage("static_filter", values["input_ready_population"], values["input_ready_population"]),
            _stage("dynamic_market", values["input_ready_population"], values["input_ready_population"]),
            _stage("dynamic_filter", values["input_ready_population"], values["dynamic_filter_eligible"]),
            _stage(
                "market_features",
                values["requested_candidates"],
                values["model_input_eligible"],
                candidate_features=values["candidate_features"],
                security_master=values["security_master"],
                history=values["history"],
                strategy_history_eligible=values["strategy_history_eligible"],
            ),
            _stage(
                "model_filter",
                values["model_input_eligible"],
                values["candidate_features"],
                model_input_eligible=values["model_input_eligible"],
            ),
            _stage("local_score", values["candidate_features"], values["full_scored"]),
            _stage("local_risk", values["full_scored"], values["full_scored"]),
            _stage("fixed_selection", values["full_scored"], seats),
            _stage("risk_review", seats, seats),
            _stage(
                "action_publish",
                seats,
                seats,
                action_executable=min(seats, values["selected_executable"]),
                action_observe=min(seats, values["selected_observe"]),
                action_unavailable=max(0, seats - values["selected_executable"] - values["selected_observe"]),
            ),
        ],
    }


def _stage(key: str, input_count: object, output_count: object, **facets: object) -> dict[str, object]:
    total = input_count if isinstance(input_count, int) else 0
    return {
        "key": key,
        "state": "completed",
        "input_count": input_count,
        "output_count": output_count,
        "metric_ranges": [],
        "facets": [{"key": name, "count": count, "total": total} for name, count in facets.items()],
        "reason_counts": [],
    }


def test_model_readiness_uses_model_stage_even_with_an_unrelated_primary_blocker():
    funnel = _funnel()
    funnel["stages"][5] = _stage("model_filter", 1401, 0, model_input_eligible=0)
    sample = _sample(1, funnel=funnel, primary_blocker="chinext_board_too_hot")
    findings = analyze_samples((sample,), strategies=(_STRATEGY,))
    assert any(finding.code == "model_input_unavailable" for finding in findings)


def test_upstream_pending_quotes_do_not_invent_a_model_input_failure():
    funnel = _funnel(model_input_eligible=0, candidate_features=0, full_scored=0)
    funnel["stages"][4].update(
        pending_count=360,
        rejected_count=0,
        failed_count=0,
        not_selected_count=0,
        duration_ms=26350,
        reason_counts=[{"reason": "stale_quote", "count": 360}],
    )
    sample = _sample(1, funnel=funnel, primary_blocker="stale_quote")
    findings = analyze_samples((sample,), strategies=(_STRATEGY,))
    assert not any(finding.code == "model_input_unavailable" for finding in findings)
    report = build_report(
        "http://127.0.0.1:5000", (sample,), findings, strategies=(_STRATEGY,), consecutive_zero_threshold=1
    )
    stage = report["samples"][0]["strategies"][_STRATEGY]["pipeline"]["stages"][4]
    assert stage["pending_count"] == 360 and stage["rejected_count"] == 0 and stage["duration_ms"] == 26350
    assert stage["reasons"] == [{"code": "stale_quote", "count": 360}]


def test_selected_profile_is_applied_to_status_and_every_current_endpoint(monkeypatch):
    requests = []

    def fetch(url, timeout):
        requests.append(url)
        return (
            {
                "recent_errors": [
                    {
                        "code": "refresh:timeout",
                        "stage": "refresh",
                        "count": 3,
                        "severity": "degraded",
                        "recovery_status": "recovered",
                        "occurred_at": "first",
                        "last_occurred_at": "last",
                        "resolved_at": "resolved",
                        "private_payload": "must-not-project",
                    }
                ]
            }
            if "/api/status" in url
            else {}
        )

    monkeypatch.setattr("scripts.runtime_diagnostics.web_health._fetch_mapping", fetch)
    samples = collect_samples(
        "http://127.0.0.1:5000",
        strategies=(_STRATEGY,),
        sample_count=1,
        interval_seconds=0,
        timeout_seconds=1,
        scoring_profile="v1",
    )
    assert len(requests) == 3 and all(url.endswith("?profile=v1") for url in requests)
    report = build_report("http://127.0.0.1:5000", samples, (), strategies=(_STRATEGY,), consecutive_zero_threshold=1)
    issue = report["samples"][0]["recent_errors"][0]
    assert issue["recovery_status"] == "recovered" and issue["count"] == 3 and issue["stage"] == "refresh"
    assert "must-not-project" not in str(report)


def _sample(
    number: int,
    *,
    funnel: Mapping[str, object] | None = None,
    freeze_status: str = "ready",
    quality_status: str | None = None,
    empty_reason: str | None = "risk_or_execution_blocked",
    projection_version: str = "projection-1",
    event_sequence: int | None = None,
    phase: str = "before_close",
    items: list[dict[str, object]] | None = None,
    include_quality_trade_date: bool = True,
    warmup: tuple[int, int, int, int] = (20, 15, 0, 5),
    warmup_timeout_count: int = 0,
    evaluated_count: int = 65,
    degraded_reasons: tuple[str, ...] = (),
    frozen: bool | None = None,
    recent_errors: list[dict[str, object]] | None = None,
    highest_final_score: float | None = 74.0,
    top_scores: list[dict[str, object]] | None = None,
    candidate_quote_age: tuple[float, float, float] = (1.0, 2.0, 3.0),
    primary_blocker: str | None = None,
    population_filter_reason_counts: Mapping[str, int] | None = None,
    root_degraded_reasons: tuple[str, ...] = (),
    predictor_batch_count: int = 1,
    population_count: int = 5500,
    model_industry_source: tuple[int, int, int, str | None] = (5300, 0, 0, None),
    include_input_quality: bool = True,
    freeze_pipeline: Mapping[str, object] | None = None,
) -> WebSample:
    quality = (
        {
            "status": quality_status or freeze_status,
            "publishable": (quality_status or freeze_status) in {"ready", "business_empty"},
            "primary_blocker": primary_blocker
            or ("ready" if freeze_status == "ready" else "history_coverage_incomplete"),
            "population_count": population_count,
            "history_required_sessions": 61,
            "population_filter_reason_counts": dict(population_filter_reason_counts or {"stale_quote": 5571}),
            "candidate_filter_reason_counts": {"history_too_short": 71},
            "candidate_transient_reason_counts": {"history_data_degraded": 12},
            "candidate_optional_reason_counts": {"research_data_degraded": 3},
            "supply_reason_counts": {"local_score_unavailable": 17},
            "pipeline": dict(funnel or _funnel()),
            "summary": {
                **({"trade_date": _TRADE_DATE} if include_quality_trade_date else {}),
                "quote_total_count": 360,
                "quote_covered_count": 360,
                "quote_missing_count": 0,
                "security_identity_missing_count": 0,
                "highest_final_score": highest_final_score,
            },
        }
        if include_input_quality and (funnel is not None or freeze_status != "not_ready")
        else None
    )
    status: dict[str, object] = {
        "schema_version": "runtime_status",
        "release": {"freeze_view_schema": "freeze_view"},
        "status": "running",
        "runtime_started": True,
        "runtime_version": "runtime-test",
        "phase": phase,
        "degraded_reasons": list(root_degraded_reasons),
        "scoring_profile": {
            "profile_id": "v2",
            "heads": {
                _STRATEGY: {
                    "profile_id": "v2",
                    "is_enable": True,
                    "model_id": "tomorrow-model",
                    "model_hash": "a" * 64,
                    "computation": {
                        "request_count": number,
                        "candidate_count": evaluated_count,
                        "predictor_batch_count": predictor_batch_count,
                        "cache_hit_count": 0,
                    },
                }
            },
        },
        "company_research": {
            "state": "idle",
            "running_codes": 0,
            "pending_codes": 0,
            "completed_batches": 2,
            "partial_batches": 1,
            "failed_batches": 0,
            "deferred_codes": 0,
            "cooldown_codes": 3,
            "retry_wait_codes": 2,
            "next_retry_seconds": 30.0,
            "gated_offer_codes": 5,
            "short_circuited_batches": 1,
            "short_circuited_codes": 8,
            "tracked_code_gates": 9,
            "evicted_code_gates": 1,
            "batch_size": 4,
            "batch_budget_seconds": 40.0,
            "success_cooldown_seconds": 60.0,
            "retry_delays_seconds": [60.0, 120.0],
            "trade_date": _TRADE_DATE,
            "tracked_strategies": 1,
            "tracked_output_codes": 12,
            "next_periodic_at": "2026-08-31T14:45:00+08:00",
            "intent_offer_count": 4,
            "periodic_offer_count": 2,
            "result_count": 3,
            "rescore_result_count": 2,
        },
        "market_data": {
            "market_feature_rows": 5500,
            "candidate_quote_cache_entries": 360,
            "candidate_quote_age": {
                "p50_seconds": candidate_quote_age[0],
                "p95_seconds": candidate_quote_age[1],
                "maximum_seconds": candidate_quote_age[2],
                "sample_count": 360,
            },
            "history_universe_rows": 360,
            "history_covered_rows": warmup[1],
            "history_coverage_ratio": warmup[1] / 360,
            "history_error_count": warmup[2] + warmup_timeout_count,
            "published_history_state": "published",
            "published_history_snapshot_hash": "b" * 64,
            "published_history_data_cutoff": _TRADE_DATE,
            "history_maintenance_state": "running" if warmup[3] else "already_current",
            "history_maintenance_stage": "downloading_codes",
            "history_maintenance_completed_units": warmup[1],
            "history_maintenance_total_units": warmup[0],
            "sources": {
                "baostock_industry": {
                    "snapshot_rows": model_industry_source[0],
                    "error_count": model_industry_source[1],
                    "timeout_count": model_industry_source[2],
                    "last_error_code": model_industry_source[3],
                }
            },
        },
        "scheduler": {"input_quality": {_STRATEGY: quality} if quality is not None else {}},
        "strategies": {
            _STRATEGY: {
                "status": freeze_status,
                "trade_date": _TRADE_DATE,
                "projection_version": projection_version,
                "coverage": {
                    "candidate_count": 360 if freeze_status == "ready" else 0,
                    "evaluated_count": evaluated_count if freeze_status == "ready" else 0,
                    "rejected_count": 295 if freeze_status == "ready" else 0,
                    "selected_count": 0,
                },
            }
        },
        "events": {"sequence": event_sequence if event_sequence is not None else number},
        "recent_errors": recent_errors or [],
    }
    freeze: dict[str, object] = {
        "schema_version": "freeze_view",
        "status": freeze_status,
        "strategy": _STRATEGY,
        "trade_date": _TRADE_DATE,
        "projection_version": projection_version,
        "degraded_reasons": list(degraded_reasons),
        **({"frozen": frozen} if frozen is not None else {}),
        "coverage": {
            "candidate_count": 360 if freeze_status == "ready" else 0,
            "evaluated_count": evaluated_count if freeze_status == "ready" else 0,
            "rejected_count": 295 if freeze_status == "ready" else 0,
            "selected_count": 0,
        },
        "selection_diagnostics": (
            {"empty_reason": empty_reason, "maximum_final_score": highest_final_score}
            if empty_reason is not None
            else {"maximum_final_score": highest_final_score}
        ),
        "top_scores": (
            top_scores
            if top_scores is not None
            else [{"scores": {"final": highest_final_score}}]
            if highest_final_score is not None
            else []
        ),
        "items": items or [],
        "pipeline": dict(freeze_pipeline) if freeze_pipeline is not None else None,
    }
    return parse_web_sample(
        number,
        f"2026-08-25T10:00:0{number}+08:00",
        status_payload=status,
        freeze_payloads={_STRATEGY: freeze},
    )


def test_restarted_freeze_record_reports_its_persisted_pipeline_without_runtime_input_quality() -> None:
    sample = _sample(
        1,
        include_input_quality=False,
        freeze_pipeline=_funnel(),
        frozen=True,
        phase="after_close",
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)
    report = build_report(
        "http://127.0.0.1:5000",
        (sample,),
        findings,
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert not any(item.code == "input_quality_persistently_missing" for item in findings)
    assert report["samples"][0]["strategies"][_STRATEGY]["pipeline"]["current_stage"] == "action_publish"


def test_repeated_input_supersession_with_missed_checkpoint_is_reported() -> None:
    sample = _sample(
        1,
        funnel=_funnel(),
        recent_errors=[
            {
                "code": "refresh:input_superseded",
                "severity": "degraded",
                "strategy": _STRATEGY,
                "stage": "refresh",
                "occurred_at": "2026-08-25T14:41:06+08:00",
                "last_occurred_at": "2026-08-25T14:50:04+08:00",
                "count": 34,
                "recovery_status": "recovered",
                "resolved_at": "2026-08-25T15:00:19+08:00",
            },
            {
                "code": "checkpoint:no_eligible_freeze",
                "severity": "error",
                "strategy": _STRATEGY,
                "stage": "checkpoint",
                "occurred_at": "2026-08-25T14:49:20+08:00",
                "last_occurred_at": "2026-08-25T14:49:38+08:00",
                "count": 5,
                "recovery_status": "active",
                "resolved_at": None,
            },
        ],
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    finding = next(item for item in findings if item.code == "scoring_input_supersession_starvation")
    assert finding.severity == "error"
    assert finding.strategy == _STRATEGY
    assert finding.evidence == {"supersession_count": 34, "checkpoint_miss_count": 5}


def test_single_input_supersession_without_checkpoint_miss_is_not_reported_as_starvation() -> None:
    sample = _sample(
        1,
        funnel=_funnel(),
        recent_errors=[
            {
                "code": "refresh:input_superseded",
                "strategy": _STRATEGY,
                "stage": "refresh",
                "count": 1,
                "recovery_status": "recovered",
            }
        ],
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert not any(item.code == "scoring_input_supersession_starvation" for item in findings)


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

    assert not any(finding.code == "pipeline_full_scored_persistently_zero" for finding in findings)


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
        _sample(number, funnel=stalled, freeze_status="not_ready", empty_reason=None) for number in range(1, 4)
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)
    codes = {finding.code for finding in findings}

    assert "pipeline_full_scored_persistently_zero" in codes
    assert "pipeline_security_master_persistently_zero" in codes
    assert "pipeline_history_persistently_zero" in codes
    assert "selected_executable_persistently_zero" not in codes


def test_persistent_model_input_zero_is_checked_against_its_direct_upstream_stage() -> None:
    stalled = _funnel(
        strategy_history_eligible=115,
        model_input_eligible=0,
        scoring_input_eligible=0,
        scoring_input_count=0,
        requested_candidates=0,
        candidate_features=0,
        candidate_quote_eligible=0,
        security_master=0,
        history=0,
        filter_pass=0,
        filter_reject=0,
        full_scored=0,
        review_eligible=0,
        action_unavailable=0,
    )
    samples = tuple(
        _sample(
            index,
            funnel=stalled,
            freeze_status="not_ready",
            quality_status="business_empty",
            empty_reason=None,
            event_sequence=index,
            predictor_batch_count=0,
        )
        for index in range(1, 4)
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)
    codes = {finding.code for finding in findings}

    assert "pipeline_model_input_eligible_persistently_zero" in codes
    assert "pipeline_scoring_input_eligible_persistently_zero" not in codes


def test_dominant_stale_market_population_is_reported_at_the_first_broken_stage() -> None:
    stalled = _funnel(
        requested_candidates=0,
        candidate_features=0,
        security_master=0,
        history=0,
        filter_pass=0,
        filter_reject=0,
        full_scored=0,
        review_eligible=0,
        action_unavailable=0,
    )
    sample = _sample(
        1,
        funnel=stalled,
        freeze_status="not_ready",
        quality_status="transient_invalid_empty",
        empty_reason=None,
        evaluated_count=0,
        highest_final_score=None,
        primary_blocker="market_population_stale",
        population_filter_reason_counts={"stale_quote": 5089},
        predictor_batch_count=0,
        population_count=5291,
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)

    finding = next(item for item in findings if item.code == "market_population_stale")
    assert finding.severity == "error"
    assert finding.evidence == {"population_count": 5291, "stale_quote_count": 5089}


def test_dominant_missing_market_liquidity_history_is_reported_at_the_first_broken_stage() -> None:
    stalled = _funnel(
        requested_candidates=0,
        candidate_features=0,
        security_master=0,
        history=0,
        filter_pass=0,
        filter_reject=0,
        full_scored=0,
        review_eligible=0,
        action_unavailable=0,
    )
    sample = _sample(
        1,
        funnel=stalled,
        freeze_status="not_ready",
        quality_status="transient_invalid_empty",
        empty_reason=None,
        evaluated_count=0,
        highest_final_score=None,
        primary_blocker="market_liquidity_history_unavailable",
        population_filter_reason_counts={"missing_liquidity_history": 5165},
        predictor_batch_count=0,
        population_count=5279,
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)

    finding = next(item for item in findings if item.code == "market_liquidity_history_unavailable")
    assert finding.severity == "error"
    assert finding.evidence == {"population_count": 5279, "missing_liquidity_history_count": 5165}


def test_model_input_gap_is_reported_at_the_first_broken_stage() -> None:
    stalled = _funnel(
        strategy_history_eligible=201,
        model_input_eligible=0,
        scoring_input_eligible=0,
        scoring_input_count=0,
        requested_candidates=0,
        candidate_features=0,
        candidate_quote_eligible=0,
        security_master=0,
        history=0,
        filter_pass=0,
        filter_reject=0,
        full_scored=0,
        review_eligible=0,
        action_unavailable=0,
    )
    sample = _sample(
        1,
        funnel=stalled,
        freeze_status="not_ready",
        quality_status="business_empty",
        empty_reason=None,
        evaluated_count=0,
        highest_final_score=None,
        primary_blocker="model_input_unavailable",
        predictor_batch_count=0,
        population_count=5291,
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)

    finding = next(item for item in findings if item.code == "model_input_unavailable")
    assert finding.severity == "error"
    assert finding.evidence == {"strategy_history_eligible": 201, "model_input_eligible": 0}


def test_model_input_gap_identifies_an_empty_current_industry_reference() -> None:
    stalled = _funnel(
        strategy_history_eligible=201,
        model_input_eligible=0,
        scoring_input_eligible=0,
        scoring_input_count=0,
        requested_candidates=0,
        candidate_features=0,
        candidate_quote_eligible=0,
        security_master=0,
        history=0,
        filter_pass=0,
        filter_reject=0,
        full_scored=0,
        review_eligible=0,
        action_unavailable=0,
    )
    sample = _sample(
        1,
        funnel=stalled,
        freeze_status="not_ready",
        quality_status="business_empty",
        empty_reason=None,
        evaluated_count=0,
        highest_final_score=None,
        primary_blocker="model_input_unavailable",
        predictor_batch_count=0,
        population_count=5291,
        model_industry_source=(0, 1, 1, "TimeoutError"),
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)

    finding = next(item for item in findings if item.code == "model_industry_reference_unavailable")
    assert finding.evidence == {
        "snapshot_rows": 0,
        "error_count": 1,
        "timeout_count": 1,
        "last_error_code": "TimeoutError",
    }


def test_research_observer_failure_is_reported_from_root_degradation() -> None:
    sample = _sample(1, root_degraded_reasons=("observer:ResearchTraceCapacityError",))

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)

    finding = next(item for item in findings if item.code == "research_observer_failed")
    assert finding.severity == "error"
    assert finding.evidence == {"error_code": "ResearchTraceCapacityError"}


def test_runtime_report_exposes_enabled_model_head_consumption() -> None:
    sample = _sample(3, predictor_batch_count=2, evaluated_count=65)

    report = build_report(
        "http://127.0.0.1:5000",
        (sample,),
        (),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert report["samples"][0]["scoring_profile"] == {
        "profile_id": "v2",
        "heads": {
            "tomorrow": {
                "profile_id": "v2",
                "is_enable": True,
                "model_id": "tomorrow-model",
                "model_hash": "a" * 64,
                "request_count": 3,
                "candidate_count": 65,
                "predictor_batch_count": 2,
                "cache_hit_count": 0,
                "score_scale": None,
                "production_authority": None,
                "training_label_cutoff": None,
            }
        },
    }


def test_nonzero_funnel_stage_regressing_to_zero_is_reported_immediately() -> None:
    samples = (
        _sample(1, funnel=_funnel(full_scored=65), event_sequence=10),
        _sample(2, funnel=_funnel(full_scored=0), event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(
        finding.code == "pipeline_regressed_to_zero"
        and finding.strategy == _STRATEGY
        and finding.evidence.get("field") == "full_scored"
        for finding in findings
    )


def test_legacy_global_history_gate_blocking_existing_scores_is_reported() -> None:
    sample = _sample(
        1,
        funnel=_funnel(history=73, full_scored=46),
        freeze_status="not_ready",
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
        _sample(2, funnel=None, freeze_status="not_ready", empty_reason=None, event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert any(finding.code == "input_quality_disappeared" for finding in findings)


def test_status_and_current_projection_mismatch_is_reported() -> None:
    sample = _sample(1)
    freeze = replace(sample.freezes[_STRATEGY], projection_version="different-projection")
    mismatched = replace(sample, freezes={_STRATEGY: freeze})

    findings = analyze_samples((mismatched,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(
        finding.code == "status_current_projection_mismatch" and finding.severity == "warning" for finding in findings
    )


def test_status_and_current_coverage_mismatch_is_reported() -> None:
    sample = _sample(1)
    freeze = sample.freezes[_STRATEGY]
    mismatched_freeze = replace(
        freeze,
        coverage=replace(freeze.coverage, selected_count=1),
        item_count=1,
    )
    mismatched = replace(sample, freezes={_STRATEGY: mismatched_freeze})

    findings = analyze_samples((mismatched,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(finding.code == "status_current_selected_count_mismatch" for finding in findings)


@pytest.mark.parametrize("selected_count,action_count,mismatch", ((0, 0, False), (2, 2, False), (3, 2, True)))
def test_six_display_slots_count_only_selected_rows(selected_count, action_count, mismatch) -> None:
    sample = _sample(1, items=[{"selected": True}] * action_count + [{"selected": False}] * (6 - action_count))
    freeze = sample.freezes[_STRATEGY]
    freeze = replace(freeze, coverage=replace(freeze.coverage, selected_count=selected_count))
    sample = replace(sample, freezes={_STRATEGY: freeze})

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(finding.code == "coverage_item_count_mismatch" for finding in findings) is mismatch


def test_ready_zero_selection_without_empty_reason_is_reported() -> None:
    findings = analyze_samples(
        (_sample(1, funnel=_funnel(), empty_reason=None),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(finding.code == "legal_empty_diagnostics_missing" for finding in findings)


def test_no_positive_net_utility_requires_at_least_one_scored_candidate() -> None:
    findings = analyze_samples(
        (_sample(1, empty_reason="no_positive_net_utility", evaluated_count=0),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert any(
        finding.code == "no_positive_net_utility_without_scored_candidates"
        and finding.severity == "error"
        and finding.evidence.get("evaluated_count") == 0
        for finding in findings
    )


def test_ready_strategy_without_scored_candidates_is_reported_immediately() -> None:
    findings = analyze_samples(
        (
            _sample(
                1,
                funnel=_funnel(full_scored=0, review_eligible=0, action_unavailable=0),
                evaluated_count=0,
                highest_final_score=None,
            ),
        ),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(
        finding.code == "scored_candidates_missing"
        and finding.severity == "error"
        and finding.evidence.get("candidate_features") == 360
        for finding in findings
    )


def test_zero_highest_score_is_reported_for_scored_strategy() -> None:
    findings = analyze_samples(
        (_sample(1, highest_final_score=0.0, phase="midday", quality_status="not_ready"),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(
        finding.code == "highest_final_score_non_positive"
        and finding.severity == "error"
        and finding.evidence.get("highest_final_score") == 0.0
        for finding in findings
    )


def test_missing_top_scores_is_reported_when_candidates_were_scored() -> None:
    findings = analyze_samples(
        (_sample(1, top_scores=[]),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(finding.code == "scored_top_scores_missing" and finding.severity == "error" for finding in findings)


def test_stale_candidate_quote_majority_is_reported_during_scoring() -> None:
    findings = analyze_samples(
        (_sample(1, candidate_quote_age=(501.0, 505.0, 512.0)),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(
        finding.code == "candidate_quotes_stale"
        and finding.severity == "error"
        and finding.evidence.get("p95_seconds") == 505.0
        and finding.evidence.get("limit_seconds") == 30.0
        for finding in findings
    )


def test_candidate_quote_age_limit_is_uniform_before_close() -> None:
    today_findings = analyze_samples(
        (_sample(1, phase="before_close", candidate_quote_age=(29.0, 31.0, 32.0)),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )
    afternoon_findings = analyze_samples(
        (_sample(1, phase="before_close", candidate_quote_age=(25.0, 29.0, 31.0)),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=3,
    )

    assert any(
        finding.code == "candidate_quotes_stale" and finding.evidence.get("limit_seconds") == 30.0
        for finding in today_findings
    )
    assert not any(finding.code == "candidate_quotes_stale" for finding in afternoon_findings)


def test_frozen_no_positive_inconsistency_is_a_controlled_degradation() -> None:
    findings = analyze_samples(
        (_sample(1, empty_reason="no_positive_net_utility", evaluated_count=0, frozen=True),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert any(
        finding.code == "no_positive_net_utility_without_scored_candidates"
        and finding.severity == "warning"
        and finding.evidence.get("frozen") is True
        for finding in findings
    )


def test_snapshot_degradation_and_company_research_are_visible_in_diagnostic_report() -> None:
    sample = _sample(
        1,
        degraded_reasons=("corporate_risk_window_unavailable", "strategy_history_coverage_partial"),
    )

    findings = analyze_samples((sample,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)
    report = build_report(
        "http://127.0.0.1:5000",
        (sample,),
        findings,
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert any(
        finding.code == "snapshot_quality_degraded" and finding.evidence.get("reason_count") == 2
        for finding in findings
    )
    assert report["samples"][0]["company_research"] == {
        "state": "idle",
        "running_codes": 0,
        "pending_codes": 0,
        "completed_batches": 2,
        "partial_batches": 1,
        "failed_batches": 0,
        "deferred_codes": 0,
        "cooldown_codes": 3,
        "retry_wait_codes": 2,
        "next_retry_seconds": 30.0,
        "gated_offer_codes": 5,
        "short_circuited_batches": 1,
        "short_circuited_codes": 8,
        "tracked_code_gates": 9,
        "evicted_code_gates": 1,
        "batch_size": 4,
        "batch_budget_seconds": 40.0,
        "success_cooldown_seconds": 60.0,
        "retry_delays_seconds": [60.0, 120.0],
        "trade_date": _TRADE_DATE,
        "tracked_strategies": 1,
        "tracked_output_codes": 12,
        "next_periodic_at": "2026-08-31T14:45:00+08:00",
        "intent_offer_count": 4,
        "periodic_offer_count": 2,
        "result_count": 3,
        "rescore_result_count": 2,
    }


def test_missing_company_research_telemetry_is_reported() -> None:
    sample = _sample(1)
    assert sample.status is not None
    missing = replace(
        sample,
        status=replace(
            sample.status,
            company_research=replace(sample.status.company_research, state=None),
        ),
    )

    findings = analyze_samples((missing,), strategies=(_STRATEGY,), consecutive_zero_threshold=1)

    assert any(finding.code == "company_research_telemetry_missing" for finding in findings)


def test_input_quality_without_trade_date_is_reported_as_invalid_shape() -> None:
    findings = analyze_samples(
        (_sample(1, funnel=_funnel(), include_quality_trade_date=False),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert any(finding.code == "input_quality_shape_invalid" for finding in findings)


def test_pipeline_missing_a_canonical_stage_is_reported_as_invalid() -> None:
    incomplete = _funnel()
    stages = incomplete["stages"]
    assert isinstance(stages, list)
    incomplete["stages"] = stages[:-1]

    findings = analyze_samples(
        (_sample(1, funnel=incomplete),),
        strategies=(_STRATEGY,),
        consecutive_zero_threshold=1,
    )

    assert any(finding.code == "pipeline_count_invalid" for finding in findings)


def test_scoring_regression_is_ignored_after_strategy_freezes() -> None:
    samples = (
        _sample(1, funnel=_funnel(full_scored=65), event_sequence=10),
        _sample(2, funnel=_funnel(full_scored=0), event_sequence=11, phase="frozen"),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert not any(finding.code == "pipeline_regressed_to_zero" for finding in findings)
    assert not any(finding.code.endswith("persistently_zero") for finding in findings)


def test_runtime_restart_splits_persistent_zero_window() -> None:
    stalled = _funnel(security_master=0, history=0, full_scored=0)
    samples = (
        _sample(1, funnel=stalled, freeze_status="not_ready", empty_reason=None, event_sequence=9),
        _sample(2, funnel=stalled, freeze_status="not_ready", empty_reason=None, event_sequence=10),
        _sample(3, funnel=stalled, freeze_status="not_ready", empty_reason=None, event_sequence=1),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=3)

    assert any(finding.code == "runtime_restart_observed" for finding in findings)
    assert not any(finding.code.endswith("persistently_zero") for finding in findings)


def test_history_archive_error_growth_is_reported_with_counter_delta() -> None:
    samples = (
        _sample(1, warmup=(100, 75, 20, 5), event_sequence=10),
        _sample(2, warmup=(105, 75, 25, 5), event_sequence=11),
    )

    findings = analyze_samples(samples, strategies=(_STRATEGY,), consecutive_zero_threshold=2)

    assert any(
        finding.code == "history_archive_errors_increased"
        and finding.severity == "warning"
        and finding.evidence.get("previous") == 20
        and finding.evidence.get("current") == 25
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
    assert report["schema_version"] == "web_recommendation_health"
    assert report["status"] == "passed"
    assert report["samples"][0]["market"]["history_archive"]["maintenance_total_units"] == 20
    assert report["samples"][0]["market"]["history_archive"]["state"] == "published"
    assert report["samples"][0]["market"]["candidate_quote_age"] == {
        "p50_seconds": 1.0,
        "p95_seconds": 2.0,
        "maximum_seconds": 3.0,
        "sample_count": 360,
    }
    strategy = report["samples"][0]["strategies"][_STRATEGY]
    assert strategy["history_required_sessions"] == 61
    assert strategy["highest_final_score"] == 74.0
    assert strategy["current_projection"]["maximum_final_score"] == 74.0
    assert strategy["current_projection"]["top_score_count"] == 1
    assert strategy["current_projection"]["highest_top_score"] == 74.0
    assert strategy["pipeline"]["current_stage"] == "action_publish"
    evidence = next(stage for stage in strategy["pipeline"]["stages"] if stage["key"] == "local_score")
    assert evidence["input_count"] == 360
    assert evidence["output_count"] == 65
    assert strategy["population_filter_reason_counts"] == {"stale_quote": 5571}
    assert strategy["candidate_filter_reason_counts"] == {"history_too_short": 71}
    assert strategy["candidate_transient_reason_counts"] == {"history_data_degraded": 12}
    assert strategy["candidate_optional_reason_counts"] == {"research_data_degraded": 3}
    assert strategy["supply_reason_counts"] == {"local_score_unavailable": 17}


@pytest.mark.parametrize("fault", (None, "batch", "count", "order", "length", "state", "negative", "population"))
def test_committed_eleven_stage_audit_is_checked_even_with_newer_live_input(fault: str | None) -> None:
    stages = [
        {
            "stage": stage.value,
            "stage_order": index + 1,
            "state": "ready",
            "input_batch_id": f"private-batch-{index}",
            "output_batch_id": f"private-batch-{index + 1}",
            "input_count": 10,
            "output_count": 10,
            "rejected_count": 0,
            "pending_count": 0,
            "failed_count": 0,
            "not_selected_count": 0,
        }
        for index, stage in enumerate(PIPELINE_STAGES)
    ]
    stages[4].update(
        latency_ms=26350,
        operation_durations=[{"operation": "quote_refresh", "duration_ms": 26350}],
        reasons=[{"code": "stale_quote", "count": 0, "private_label": "must-not-project"}],
    )
    if fault == "batch":
        stages[9]["input_batch_id"] = "other-private-batch"
    elif fault == "count":
        stages[9]["input_count"] = 11
    elif fault == "order":
        stages[9]["stage_order"] = 1
    elif fault == "length":
        stages.pop()
    elif fault == "state":
        stages[9]["state"] = {"unexpected": "object"}
    elif fault == "negative":
        stages[9]["pending_count"] = -1
    elif fault == "population":
        stages[9]["failed_count"] = 1
    committed = {**_funnel(), "stage_snapshots": stages}
    sample = _sample(1, freeze_pipeline=committed, frozen=True, phase="after_close")
    findings = analyze_samples((sample,), strategies=(_STRATEGY,))
    assert ("committed_pipeline_audit_invalid" in {finding.code for finding in findings}) == (fault is not None)
    report = build_report("http://127.0.0.1:5000", (sample,), (), strategies=(_STRATEGY,), consecutive_zero_threshold=1)
    summary = report["samples"][0]["strategies"][_STRATEGY]
    audit = summary["committed_pipeline"]["stage_audit"]
    assert audit["observed"] and audit["continuous"] == (fault is None)
    assert audit["stage_count"] == len(stages)
    assert audit["stages"][4]["duration_ms"] == 26350
    assert audit["stages"][4]["operation_durations"] == [{"operation": "quote_refresh", "duration_ms": 26350}]
    assert audit["stages"][0]["duration_ms"] is None
    assert summary["current_projection"]["frozen"] is True
    assert summary["pipeline"]["stage_audit"]["observed"] is False
    assert "private-batch" not in str(report)
    assert "must-not-project" not in str(report)


def test_missing_committed_audit_is_not_reconstructed_from_live_input() -> None:
    sample = _sample(1, freeze_pipeline=_funnel())
    report = build_report("http://127.0.0.1:5000", (sample,), (), strategies=(_STRATEGY,), consecutive_zero_threshold=1)
    audit = report["samples"][0]["strategies"][_STRATEGY]["committed_pipeline"]["stage_audit"]
    assert audit == {"observed": False, "stage_count": 0, "continuous": None, "invalid_fields": [], "stages": []}
