from __future__ import annotations

from concurrent.futures import Future
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from trader.application.tomorrow_research_trace import (
    AsyncTomorrowResearchTraceRecorder,
    InMemoryTomorrowResearchTraceStore,
    research_trace_payload,
)
from trader.application.tomorrow_research_trace_types import (
    TomorrowCandidateResearchTrace,
    TomorrowDecisionCandidateTrace,
    TomorrowDecisionSetTrace,
    TomorrowHardFilterAggregate,
    TomorrowResearchTrace,
    TomorrowResearchTraceCapture,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
EVALUATED_AT = datetime(2026, 7, 28, 14, 50, tzinfo=SHANGHAI)


def _candidate(code: str = "600001") -> TomorrowCandidateResearchTrace:
    return TomorrowCandidateResearchTrace(
        code=code,
        board="main",
        industry="industry",
        feature_input_hash="c" * 64,
        candidate_components=(("liquidity", 81.0), ("trend", 79.0)),
        missing_mask=(),
        coverage_ratio=1.0,
        board_reliability=1.0,
        candidate_score=80.0,
        candidate_rank=1,
        production_top120=True,
        optimistic_upper_bound=None,
        upper_bound_status="not_computed",
        upper_bound_protected=False,
        pruning_reason="",
    )


def _decision(code: str = "600001") -> TomorrowDecisionCandidateTrace:
    return TomorrowDecisionCandidateTrace(
        code=code,
        components=(("trend", 82.0),),
        component_coverage_ratio=1.0,
        base_score=82.0,
        local_risk_codes=(),
        local_risk_penalty=0.0,
        local_score=82.0,
        reused_deepseek_facts=False,
        fusion_applied=False,
        deepseek_risk_codes=(),
        deepseek_risk_penalty=0.0,
        final_score=82.0,
        action="executable",
        downside_status="pass",
        downside_reasons=(),
        setup_type="trend_unconfirmed",
        selected=True,
        rank=1,
        board_rank=1,
        skip_reason="",
    )


def _decision_set(variant: str, version: str) -> TomorrowDecisionSetTrace:
    return TomorrowDecisionSetTrace(
        variant=variant,
        decision_version=version,
        schema_version="decision_epoch_v1",
        strategy_version="strategy-v1",
        fusion_version="fusion-v1",
        candidates=(_decision(),),
    )


def _trace(
    *,
    input_version: str = "native-input:001",
    local_version: str = "local-001",
) -> TomorrowResearchTrace:
    return TomorrowResearchTrace(
        evaluated_at=EVALUATED_AT,
        trade_date=date(2026, 7, 28),
        phase="afternoon",
        input_version=input_version,
        input_manifest_hash="a" * 64,
        data_version="data-v1",
        config_version="config-v1",
        rule_versions=("rules-v1",),
        hard_filter_aggregate_hash="b" * 64,
        received_population_by_board=(("main", 2),),
        hard_filter_aggregates=(TomorrowHardFilterAggregate("main", "st_or_delisting", 1),),
        source_coverage_status="complete",
        source_failure_categories=(),
        passed_candidates=(_candidate(),),
        production_local=_decision_set("production_local", local_version),
        research_shadow=_decision_set("research_shadow", local_version),
        shadow_mode="control_copy",
        baseline_snapshot_id="baseline-001",
        deepseek_request_delta=0,
    )


def test_research_trace_is_immutable_validated_and_has_stable_payload() -> None:
    trace = _trace()

    assert research_trace_payload(trace) == research_trace_payload(trace)
    assert b"production_local" in research_trace_payload(trace)
    assert b"research_shadow" in research_trace_payload(trace)

    try:
        trace.input_version = "changed"  # type: ignore[misc]
    except (AttributeError, TypeError):
        pass
    else:
        raise AssertionError("research trace must be immutable")


def test_store_is_idempotent_and_conflicts_never_replace_original() -> None:
    store = InMemoryTomorrowResearchTraceStore()
    original = _trace()

    assert store.record(original).status == "recorded"
    assert store.record(original).status == "duplicate"
    assert store.record(_trace(local_version="local-002")).status == "conflict"

    retained = store.get(original.input_version)
    assert retained == original
    status = store.status()
    assert status.attempts == 3
    assert status.recorded == 1
    assert status.duplicate == 1
    assert status.conflict == 1


def test_store_enforces_payload_record_and_total_byte_limits() -> None:
    trace = _trace()
    payload_bytes = len(research_trace_payload(trace))
    payload_limited = InMemoryTomorrowResearchTraceStore(maximum_payload_bytes=payload_bytes - 1)
    record_limited = InMemoryTomorrowResearchTraceStore(maximum_records=1)
    byte_limited = InMemoryTomorrowResearchTraceStore(
        maximum_payload_bytes=payload_bytes,
        maximum_total_bytes=payload_bytes,
    )

    assert payload_limited.record(trace).status == "payload_too_large"
    assert record_limited.record(trace).status == "recorded"
    assert record_limited.record(_trace(input_version="native-input:002")).status == "capacity_reached"
    assert byte_limited.record(trace).status == "recorded"
    assert byte_limited.record(_trace(input_version="native-input:002")).status == "capacity_reached"


class _RejectingExecutor:
    def submit(self, function, /, *args, **kwargs):
        del function, args, kwargs
        return None


class _ImmediateExecutor:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error

    def submit(self, function, /, *args, **kwargs):
        future: Future[object] = Future()
        if self.error is not None:
            future.set_exception(self.error)
        else:
            future.set_result(function(*args, **kwargs))
        return future


def test_async_recorder_uses_non_blocking_bounded_submission_and_explicit_failures() -> None:
    store = InMemoryTomorrowResearchTraceStore()
    rejected = AsyncTomorrowResearchTraceRecorder(store, _RejectingExecutor())
    failed = AsyncTomorrowResearchTraceRecorder(store, _ImmediateExecutor(error=OSError("disk details")))
    rejected_capture = TomorrowResearchTraceCapture(SimpleNamespace(input_version="native-input:002"), "baseline-002")
    failed_capture = TomorrowResearchTraceCapture(SimpleNamespace(input_version="native-input:003"), "baseline-003")

    assert rejected.enqueue(rejected_capture).status == "queue_full"
    assert failed.enqueue(failed_capture).status == "queued"

    assert rejected.status().queue_full == 1
    assert rejected.status().last_failure == "queue_full"
    assert failed.status().worker_failed == 1
    assert failed.status().last_failure == "OSError"


def test_research_payload_does_not_include_rejected_stock_identity() -> None:
    payload = research_trace_payload(_trace())

    assert b"600001" in payload
    assert b"600999" not in payload
