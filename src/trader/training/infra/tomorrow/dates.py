"""Effective date counts for diagnostic and current Tomorrow probability heads."""

from dataclasses import dataclass

from trader.training.domain.v1.learned_rank import V1TrainingSettings
from trader.training.infra.tomorrow.settings import TomorrowFitSettings

PRODUCTION_CALIBRATION_DAYS = 20


@dataclass(frozen=True, slots=True)
class TomorrowDateWindows:
    fitting: int
    early: int = 0
    calibration: int = 20
    assessment: int = 20
    embargo: int = 5

    @property
    def reserve(self) -> int:
        before_calibration = self.early + self.embargo * (2 if self.early else 1)
        return before_calibration + max(PRODUCTION_CALIBRATION_DAYS, self.calibration + self.embargo + self.assessment)


def TomorrowWindows(settings: TomorrowFitSettings) -> TomorrowDateWindows:
    parameters = settings.parameters
    if isinstance(parameters, V1TrainingSettings):
        return TomorrowDateWindows(
            settings.fit_dates, parameters.early_stopping_dates, parameters.calibration_dates,
            parameters.validation_dates, parameters.embargo_dates,
        )
    return TomorrowDateWindows(settings.fit_dates)
