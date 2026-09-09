from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.domain.research.candidate_recall_ledger import (
    CANDIDATE_RECALL_STAGES,
    CandidateRecallDayTrace,
    CandidateRecallDownstreamTrace,
    CandidateRecallStageLatency,
)


def _latencies() -> tuple[CandidateRecallStageLatency, ...]:
    return tuple(
        CandidateRecallStageLatency(stage, float(index)) for index, stage in enumerate(CANDIDATE_RECALL_STAGES)
    )


def test_candidate_recall_trace_requires_fixed_monotonic_cumulative_latency() -> None:
    trace = CandidateRecallDayTrace(date(2024, 1, 2), "a" * 64, "b" * 64, (), _latencies())

    with pytest.raises(ValueError, match="identity"):
        replace(trace, latency_evidence_hash="missing")
    with pytest.raises(ValueError, match="fixed order"):
        replace(trace, stage_latencies=trace.stage_latencies[:-1])
    with pytest.raises(ValueError, match="monotonic"):
        replace(
            trace,
            stage_latencies=(
                *trace.stage_latencies[:2],
                replace(trace.stage_latencies[2], cumulative_latency_ms=0.5),
                *trace.stage_latencies[3:],
            ),
        )


def test_candidate_recall_downstream_trace_requires_reason_only_for_rejection() -> None:
    rejected = CandidateRecallDownstreamTrace(
        trade_date=date(2024, 1, 2),
        code="600000",
        dataset_row_hash="b" * 64,
        first_rejection_boundary="risk",
        rejection_reasons=("risk_veto",),
    )

    with pytest.raises(ValueError, match="boundary and reasons"):
        replace(rejected, rejection_reasons=())
    with pytest.raises(ValueError, match="boundary and reasons"):
        replace(rejected, first_rejection_boundary="selected")
