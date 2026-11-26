"""A V3 probability publication with full diagnostic and current fitting dates."""

from datetime import date, timedelta
from pathlib import Path

from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.tomorrow_probability import TomorrowDiagnosticAssessment
from trader.training.infra.tomorrow.artifact import TomorrowProbabilityArtifact, TomorrowFeatureNames
from trader.training.infra.tomorrow.dates import PRODUCTION_CALIBRATION_DAYS, TomorrowWindows
from trader.training.infra.tomorrow.settings import LoadTomorrowFitSettings
from trader.training.infra.tomorrow.v3.model import LogisticTomorrowModel


def TomorrowV3ArtifactFixture(project_root: Path) -> TomorrowProbabilityArtifact:
    settings = LoadTomorrowFitSettings(project_root, "v3")
    windows = TomorrowWindows(settings)
    days = tuple(date(2022, 1, 1) + timedelta(days=i) for i in range(windows.fitting + windows.reserve))
    assess_begin = len(days) - windows.assessment
    diag_cal_end = assess_begin - windows.embargo
    diag_cal_begin = diag_cal_end - windows.calibration
    production_fit_end = len(days) - PRODUCTION_CALIBRATION_DAYS - windows.embargo
    width = len(TomorrowFeatureNames("v3"))
    return TomorrowProbabilityArtifact(
        "v3", PredictionTarget(Strategy.TOMORROW, 500), "a" * 64, settings.identity,
        days[production_fit_end - windows.fitting:production_fit_end], days[-PRODUCTION_CALIBRATION_DAYS:],
        0., 1., LogisticTomorrowModel(0., (0.1,) * width, (0.,) * width, (1.,) * width), None,
        fit_settings_hash=settings.fitting_identity, early_stopping_dates=(), diagnostic=TomorrowDiagnosticAssessment(
            "d" * 64, days[:windows.fitting], (), days[diag_cal_begin:diag_cal_end], days[assess_begin:], 1, 0, 0.2, 0, 1, 0,
        ),
    )
