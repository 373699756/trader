from __future__ import annotations

from trader.application.latency import LatencyWaterfall


class _Monotonic:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_latency_waterfall_is_bounded_and_exposes_only_aggregates() -> None:
    monotonic = _Monotonic()
    waterfall = LatencyWaterfall(sample_capacity=2, trace_capacity=2, monotonic=monotonic)

    waterfall.plan("secret-cycle-1", "full_market")
    monotonic.advance(0.010)
    waterfall.enter("secret-cycle-1")
    waterfall.record_duration("external_source", 20.0)
    waterfall.record_duration("external_source", 40.0)
    waterfall.finish("secret-cycle-1", outcome="success")

    waterfall.plan("secret-cycle-2", "candidate_quotes")
    waterfall.finish("secret-cycle-2", outcome="timeout")
    waterfall.plan("secret-cycle-3", "score")
    waterfall.finish("secret-cycle-3", outcome="superseded")

    status = waterfall.status()

    assert status["trace_capacity"] == 2
    assert status["sample_capacity"] == 2
    assert status["planned_count"] == 3
    assert status["completed_count"] == 1
    assert status["timeout_count"] == 1
    assert status["superseded_count"] == 1
    assert status["dropped_count"] == 0
    assert status["stages"]["queue_wait"] == {
        "sample_count": 1,
        "p50_ms": 10.0,
        "p95_ms": 10.0,
        "maximum_ms": 10.0,
    }
    assert status["stages"]["external_source"] == {
        "sample_count": 2,
        "p50_ms": 20.0,
        "p95_ms": 40.0,
        "maximum_ms": 40.0,
    }
    assert status["stages"]["cycle_total:full_market"] == {
        "sample_count": 1,
        "p50_ms": 10.0,
        "p95_ms": 10.0,
        "maximum_ms": 10.0,
    }
    assert "secret-cycle-1" not in repr(status)


def test_latency_waterfall_drops_oldest_unfinished_trace_at_capacity() -> None:
    waterfall = LatencyWaterfall(sample_capacity=4, trace_capacity=1)

    waterfall.plan("old", "full_market")
    waterfall.plan("new", "score")

    status = waterfall.status()
    assert status["active_trace_count"] == 1
    assert status["dropped_count"] == 1


def test_latency_waterfall_bounds_distinct_stage_names() -> None:
    waterfall = LatencyWaterfall(sample_capacity=4, stage_capacity=2)

    waterfall.record_duration("stage-a", 1.0)
    waterfall.record_duration("stage-b", 2.0)
    waterfall.record_duration("stage-c", 3.0)

    status = waterfall.status()
    assert status["stage_capacity"] == 2
    assert status["dropped_stage_count"] == 1
    assert set(status["stages"]) == {"stage-b", "stage-c"}


def test_latency_waterfall_counts_each_trace_outcome_only_once() -> None:
    waterfall = LatencyWaterfall()

    waterfall.plan("cycle", "score")
    waterfall.finish("cycle", outcome="timeout")
    waterfall.finish("cycle", outcome="failed")
    waterfall.finish("unknown", outcome="success")

    status = waterfall.status()
    assert status["timeout_count"] == 1
    assert status["failed_count"] == 0
    assert status["completed_count"] == 0
