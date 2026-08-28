from __future__ import annotations

from dataclasses import replace

from tests.unit.application.research.test_shadow_models import _labeled_day, _RecordingTrainer
from trader.application.research.cost_aware_selection import ScoreTomorrowCostAwareSelection
from trader.application.research.shadow_models import ScoreTomorrowShadowModels


def test_cost_aware_report_covers_every_shadow_prediction_and_model_family() -> None:
    shadow = _shadow_report()

    report = ScoreTomorrowCostAwareSelection().build(shadow)

    expected_days = {
        (prediction.prediction_date, prediction.horizon, prediction.window_mode, model_family)
        for prediction in shadow.predictions
        for model_family in ("linear", "lightgbm")
    }
    assert {
        (day.prediction_date, day.horizon, day.window_mode, day.model_family) for day in report.days
    } == expected_days
    expected_rows = 2 * len(shadow.predictions)
    assert sum(len(day.evaluations) for day in report.days) == expected_rows
    assert report.parent_report_hash == shadow.content_hash
    assert report.status == "exploratory"
    assert report.production_authority is False


def test_d25_uses_previous_selection_only_for_maintenance() -> None:
    shadow = _shadow_report()
    d25_dates = sorted({item.prediction_date for item in shadow.predictions if item.horizon == "d25"})
    assert len(d25_dates) >= 2
    first, second = d25_dates[:2]
    rewritten = []
    for prediction in shadow.predictions:
        value = -0.01
        if prediction.horizon == "d25" and prediction.prediction_date == first:
            value = 0.003
        elif prediction.horizon == "d25" and prediction.prediction_date == second:
            value = 0.001
        rewritten.append(replace(prediction, linear_net_excess=value, lightgbm_net_excess=value))
    report = ScoreTomorrowCostAwareSelection().build(replace(shadow, predictions=tuple(rewritten)))

    second_days = tuple(day for day in report.days if day.horizon == "d25" and day.prediction_date == second)
    assert second_days
    assert all(day.selected_codes for day in second_days)
    assert all(item.incumbent for day in second_days for item in day.evaluations if item.selected_rank is not None)


def test_tomorrow_never_carries_incumbent_state_between_days() -> None:
    report = ScoreTomorrowCostAwareSelection().build(_shadow_report())

    assert all(not item.incumbent for day in report.days if day.horizon == "tomorrow" for item in day.evaluations)


def _shadow_report():
    days = tuple(day for index in range(70) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))
    return ScoreTomorrowShadowModels((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(days)
