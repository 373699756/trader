from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trader.recommendation.application.pipeline.quality_check.pipeline_status import (
    build_first_nine_stage_snapshots,
)
from trader.recommendation.domain.evidence.pipeline import StageState
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
