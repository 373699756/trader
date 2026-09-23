from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.quality_check.pipeline_status import (
    build_first_nine_stage_snapshots,
)
from trader.recommendation.domain.evidence.pipeline import StageState
from trader.recommendation.domain.market.eligibility import (
    IssuerEligibilityBatch,
    IssuerEligibilityReason,
    IssuerEligibilityReasonCount,
)
from trader.recommendation.domain.selection.scored_selection import ScoredCandidateStageCounts


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _counts(
    *,
    issuer: int,
    input_ready: int,
    dynamic: int,
) -> ScoredCandidateStageCounts:
    return ScoredCandidateStageCounts(issuer, input_ready, dynamic, dynamic, dynamic, dynamic, dynamic)


def test_zero_input_stages_are_not_ready_instead_of_completed() -> None:
    snapshots = build_first_nine_stage_snapshots(
        _counts(issuer=0, input_ready=0, dynamic=0),
        batch_id="batch",
        as_of=datetime(2026, 9, 23, 10, 0, tzinfo=SHANGHAI),
        population_count=0,
        candidate_feature_count=0,
    )

    assert all(snapshot.state is StageState.NOT_READY for snapshot in snapshots)


def test_filtering_all_ready_inputs_is_a_completed_empty_result() -> None:
    snapshots = build_first_nine_stage_snapshots(
        _counts(issuer=10, input_ready=10, dynamic=0),
        batch_id="batch",
        as_of=datetime(2026, 9, 23, 10, 0, tzinfo=SHANGHAI),
        population_count=10,
        candidate_feature_count=0,
    )

    assert snapshots[6].input_count == 10
    assert snapshots[6].output_count == 0
    assert snapshots[6].state is StageState.READY


def test_level_one_snapshot_includes_prefiltered_registry_exclusions() -> None:
    snapshots = build_first_nine_stage_snapshots(
        _counts(issuer=10, input_ready=10, dynamic=10),
        batch_id="batch",
        as_of=datetime(2026, 9, 23, 10, 0, tzinfo=SHANGHAI),
        population_count=10,
        candidate_feature_count=10,
        issuer_eligibility=IssuerEligibilityBatch(
            13,
            10,
            (IssuerEligibilityReasonCount(IssuerEligibilityReason.HISTORICAL_ST, 3),),
        ),
    )

    level_one = snapshots[3]
    assert level_one.input_count == 13
    assert level_one.output_count == 10
    assert level_one.rejected_count == 3
    assert [(item.code, item.count) for item in level_one.reasons] == [("historical_st", 3)]


def test_dynamic_input_gap_is_data_pending_instead_of_source_failed() -> None:
    snapshots = build_first_nine_stage_snapshots(
        _counts(issuer=10, input_ready=4, dynamic=3),
        batch_id="batch",
        as_of=datetime(2026, 9, 23, 10, 0, tzinfo=SHANGHAI),
        population_count=10,
        candidate_feature_count=3,
        data_pending_count=6,
        refresh_pending_count=2,
    )

    dynamic_collection = snapshots[4]
    assert dynamic_collection.pending_count == 6
    assert dynamic_collection.failed_count == 0
    assert [(item.code, item.count) for item in dynamic_collection.reasons] == [
        ("data_pending", 4),
        ("refresh_pending", 2),
    ]


def test_quality_pending_does_not_appear_as_completed_zero_scoring() -> None:
    snapshots = build_first_nine_stage_snapshots(
        _counts(issuer=10, input_ready=10, dynamic=10),
        batch_id="batch",
        as_of=datetime(2026, 9, 23, 10, 0, tzinfo=SHANGHAI),
        population_count=10,
        candidate_feature_count=10,
        quality_ready_count=0,
    )

    quality = snapshots[8]
    assert (quality.input_count, quality.output_count, quality.pending_count) == (10, 0, 10)
    assert quality.state is StageState.NOT_READY
