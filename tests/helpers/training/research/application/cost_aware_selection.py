from __future__ import annotations
from tests.helpers.training.research.application.shadow_model_evaluation import _labeled_day, _RecordingTrainer
from trader.training.application.shadow_model_evaluation import TomorrowShadowModelEvaluator


def _shadow_report():
    days = tuple(day for index in range(70) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))
    return TomorrowShadowModelEvaluator((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(days)
