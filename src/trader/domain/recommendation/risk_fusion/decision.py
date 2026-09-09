"""Typed risk and prediction-uncertainty decisions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_FACT_ID = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class PredictionInterval:
    lower_excess_return: float
    upper_excess_return: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.lower_excess_return)
            or not math.isfinite(self.upper_excess_return)
            or self.lower_excess_return > self.upper_excess_return
        ):
            raise ValueError("prediction interval is invalid")


@dataclass(frozen=True)
class UncertaintyAssessment:
    severe_loss_probability: float | None
    prediction_interval: PredictionInterval | None
    model_disagreement: float | None
    training_window_disagreement: float | None
    ood_distance: float | None
    missing_uncertainty: float | None

    def __post_init__(self) -> None:
        probability = self.severe_loss_probability
        if probability is not None and (not math.isfinite(probability) or not 0.0 <= probability <= 1.0):
            raise ValueError("severe-loss probability must be in [0, 1]")
        values = (
            self.model_disagreement,
            self.training_window_disagreement,
            self.ood_distance,
            self.missing_uncertainty,
        )
        if any(value is not None and (not math.isfinite(value) or value < 0.0) for value in values):
            raise ValueError("uncertainty values must be finite and non-negative")
        if self.missing_uncertainty is not None and self.missing_uncertainty > 1.0:
            raise ValueError("missing uncertainty must be in [0, 1]")


@dataclass(frozen=True)
class RiskDecision:
    code: str
    penalty_points: float
    veto: bool
    structured_fact_ids: tuple[str, ...]
    uncertainty: UncertaintyAssessment

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("risk decision code must contain exactly six digits")
        if (
            not math.isfinite(self.penalty_points)
            or not 0.0 <= self.penalty_points <= 30.0
            or not isinstance(self.veto, bool)
        ):
            raise ValueError("risk decision penalty or veto is invalid")
        facts = tuple(sorted(set(self.structured_fact_ids)))
        if len(facts) != len(self.structured_fact_ids) or any(_FACT_ID.fullmatch(item) is None for item in facts):
            raise ValueError("risk decision structured facts are invalid")
        if (self.penalty_points > 0.0 or self.veto) and not facts:
            raise ValueError("risk penalty and veto require structured facts")
        object.__setattr__(self, "structured_fact_ids", facts)


__all__ = ["PredictionInterval", "RiskDecision", "UncertaintyAssessment"]
