from __future__ import annotations

import pytest

from tests.unit.application.research.test_shadow_models import _labeled_day, _RecordingTrainer
from trader.application.research.shadow_model_ports import ShadowFitRequest
from trader.application.research.shadow_models import ScoreTomorrowShadowModels
from trader.infra.research.lightgbm_shadow import LightGbmShadowTrainer
from trader.infra.research.shadow_model_artifacts import ShadowModelArtifactConflictError, ShadowModelArtifactStore


@pytest.mark.parametrize("objective", ("net_excess", "severe_loss"))
def test_lightgbm_shadow_trainer_is_shallow_deterministic_and_hashable(objective: str) -> None:
    train_x = tuple((float(index), float(index % 5)) for index in range(80))
    labels = tuple(
        (1.0 if index % 7 == 0 else 0.0) if objective == "severe_loss" else index * 0.01 - (index % 5) * 0.02
        for index in range(80)
    )
    request = ShadowFitRequest(
        objective=objective,
        feature_names=("signal", "cycle"),
        train_x=train_x,
        train_y=labels,
        validation_x=train_x[-20:],
        validation_y=labels[-20:],
        calibration_x=train_x[-10:],
        prediction_x=((81.0, 1.0), (82.0, 2.0)),
        seed=20260828,
    )
    trainer = LightGbmShadowTrainer()

    first = trainer.fit_predict(request)
    second = trainer.fit_predict(request)

    assert first == second
    assert first.model_family == "lightgbm"
    assert len(first.model_hash) == 64
    assert len(first.calibration_predictions) == 10
    assert len(first.prediction_predictions) == 2
    if objective == "severe_loss":
        assert all(0.0 <= value <= 1.0 for value in first.prediction_predictions)


def test_shadow_report_artifact_is_idempotent_and_tamper_evident(tmp_path) -> None:
    days = tuple(day for index in range(66) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))
    report = ScoreTomorrowShadowModels((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(days)
    store = ShadowModelArtifactStore(tmp_path)

    assert store.seal(report) == report.content_hash
    assert store.seal(report) == report.content_hash
    later_days = tuple(
        day for index in range(67) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25"))
    )
    later_report = ScoreTomorrowShadowModels((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(
        later_days
    )
    assert store.seal(later_report) == later_report.content_hash
    assert len(tuple(tmp_path.glob("**/shadow-report.json"))) == 2
    window = f"{report.training_window_start.isoformat()}_{report.training_window_end.isoformat()}"
    artifact = tmp_path / report.spec_hash / window / "shadow-report.json"
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace('"cost_bps":20', '"cost_bps":21'), encoding="utf-8"
    )

    with pytest.raises(ShadowModelArtifactConflictError, match="hash or schema"):
        store.seal(report)
