from __future__ import annotations

from datetime import date, timedelta

import pytest

from trader.application.research.tomorrow_joint import (
    TomorrowJointProfileBatch,
    TomorrowJointSourceRow,
    align_tomorrow_joint_batches,
    fit_tomorrow_joint_research,
    produce_tomorrow_joint_predictions,
)


def _source_row(
    profile_id: str,
    day_index: int,
    code_index: int,
    *,
    order: int | None = None,
    prediction: float | None = None,
    actual: float | None = None,
) -> TomorrowJointSourceRow:
    trade_date = date(2025, 1, 1) + timedelta(days=day_index)
    signal = (code_index - 2.0) / 100.0
    multiplier = {"v1": 0.8, "v2": -5.0, "c3": 1.2}[profile_id]
    return TomorrowJointSourceRow(
        trade_date=trade_date,
        code=f"60{code_index:04d}",
        candidate_order=code_index if order is None else order,
        label_matured_at=trade_date + timedelta(days=1),
        predicted_net_excess_20bp=signal * multiplier if prediction is None else prediction,
        actual_net_excess_20bp=signal if actual is None else actual,
        actual_net_excess_50bp=(signal if actual is None else actual) - 0.003,
        severe_loss=signal < -0.04,
    )


def _batch(profile_id: str, rows: tuple[TomorrowJointSourceRow, ...]) -> TomorrowJointProfileBatch:
    return TomorrowJointProfileBatch(profile_id=profile_id, rows=rows)


def _complete_batches(start_day: int, days: int) -> tuple[TomorrowJointProfileBatch, ...]:
    return tuple(
        _batch(
            profile_id,
            tuple(
                _source_row(profile_id, start_day + day_index, code_index)
                for day_index in range(days)
                for code_index in range(1, 5)
            ),
        )
        for profile_id in ("v1", "v2", "c3")
    )


def test_alignment_uses_strict_common_intersection_and_reports_coverage_loss() -> None:
    v1_rows = tuple(_source_row("v1", 0, code) for code in (1, 2, 3))
    v2_rows = tuple(_source_row("v2", 0, code) for code in (1, 2))
    c3_rows = tuple(_source_row("c3", 0, code) for code in (1, 2, 4))

    dataset = align_tomorrow_joint_batches((_batch("v1", v1_rows), _batch("v2", v2_rows), _batch("c3", c3_rows)))

    assert tuple(row.code for row in dataset.rows) == ("600001", "600002")
    assert dataset.coverage.union_rows == 4
    assert dataset.coverage.common_rows == 2
    by_profile = {item.profile_id: item for item in dataset.coverage.profiles}
    assert by_profile["v1"].missing_from_union == 1
    assert by_profile["v1"].dropped_outside_common == 1
    assert by_profile["v2"].missing_from_union == 2
    assert by_profile["v2"].dropped_outside_common == 0
    assert by_profile["c3"].missing_from_union == 1
    assert by_profile["c3"].dropped_outside_common == 1


@pytest.mark.parametrize("mismatch", ["candidate_order", "mature_label"])
def test_alignment_rejects_common_row_metadata_mismatch(mismatch: str) -> None:
    v1 = _source_row("v1", 0, 1)
    v2 = _source_row("v2", 0, 1, order=9 if mismatch == "candidate_order" else None)
    c3 = _source_row("c3", 0, 1, actual=0.99 if mismatch == "mature_label" else None)

    with pytest.raises(ValueError, match="metadata and mature labels"):
        align_tomorrow_joint_batches((_batch("v1", (v1,)), _batch("v2", (v2,)), _batch("c3", (c3,))))


def test_research_fit_and_prediction_keep_one_non_production_joint_output() -> None:
    result = fit_tomorrow_joint_research(
        training_batches=_complete_batches(0, 10),
        tuning_batches=_complete_batches(20, 5),
    )
    model = next(item for item in result.candidate_family.candidates if item.candidate_id == "v1_c3")
    inference = produce_tomorrow_joint_predictions(model, _complete_batches(30, 2))

    assert result.production_authority is False
    assert result.candidate_family.selection_status == "portfolio_evidence_required"
    assert model.weights.v2 == 0.0
    assert len(inference.predictions) == 8
    assert len({(item.trade_date, item.code) for item in inference.predictions}) == 8
    assert all(item.prediction_semantics == "pre_base_score_cost_adjusted_net_excess" for item in inference.predictions)
    assert inference.coverage.common_rows == 8


def test_batch_rejects_score_level_or_unmatured_inputs() -> None:
    row = _source_row("v1", 0, 1)

    with pytest.raises(ValueError, match="prediction semantics"):
        TomorrowJointProfileBatch(
            profile_id="v1",
            rows=(row,),
            prediction_semantics="base_score",  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="mature"):
        TomorrowJointSourceRow(
            trade_date=row.trade_date,
            code=row.code,
            candidate_order=row.candidate_order,
            label_matured_at=row.trade_date,
            predicted_net_excess_20bp=row.predicted_net_excess_20bp,
            actual_net_excess_20bp=row.actual_net_excess_20bp,
            actual_net_excess_50bp=row.actual_net_excess_50bp,
            severe_loss=row.severe_loss,
        )
