"""Apply a profile's Tomorrow model to one prepared market batch."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from trader.recommendation.application.ports.loaded_profile import (
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringDeadlineError,
    ScoringHeadRuntimeStatus,
    TomorrowProbability,
)
from trader.recommendation.domain.candidate.filters import BoardForSnapshot
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.tomorrow_probability import (
    BuildTomorrowFeatures,
    TomorrowProbabilityPredictor,
)
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.outcomes.tomorrow_target import TOMORROW_CLOSE_GAIN_TARGET
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import (
    ScoringProfileId,
    TOMORROW_PROBABILITY_SCORE_SCALE,
)

@dataclass(frozen=True, slots=True)
class TomorrowScoringPlan:
    """The input and prediction work owned by one Tomorrow head."""

    profile_id: ScoringProfileId
    history_required_sessions: int
    build_features: Callable[[FeatureSnapshot], tuple[float, ...] | None]
    batch_size: int | None = None


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityCapability:
    plan: TomorrowScoringPlan
    predictor: TomorrowProbabilityPredictor | None
    target: PredictionTarget | None = None
    monotonic: Callable[[], float] = field(default=time.monotonic, kw_only=True)

    def __post_init__(self) -> None:
        if self.target is not None and self.target.strategy is not Strategy.TOMORROW:
            raise ValueError("Tomorrow scoring requires a Tomorrow prediction target")

    @property
    def history_required_sessions(self) -> int:
        return self.plan.history_required_sessions

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool:
        return self.predictor is not None and self.plan.build_features(feature) is not None

    def score(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> ModelScoreBatch:
        predictor = self.predictor
        if predictor is None:
            raise RuntimeError("tomorrow_probability_model_unavailable")
        deadline = (
            self.monotonic() + context.time_budget_seconds
            if self.plan.batch_size is not None and context is not None and context.time_budget_seconds is not None
            else None
        )
        self._check_deadline(deadline)
        codes: list[str] = []
        vectors: list[tuple[float, ...]] = []
        missing: list[str] = []
        for feature in sorted(features, key=lambda item: item.quote.code):
            vector = self.plan.build_features(feature)
            if vector is None:
                missing.append(feature.quote.code)
                continue
            codes.append(feature.quote.code)
            vectors.append(vector)
        self._check_deadline(deadline)
        probabilities = self._predict_probabilities(predictor, tuple(vectors), deadline)
        predictions = tuple(
            TomorrowProbability(code, probability)
            for code, probability in zip(codes, probabilities, strict=True)
        )
        return ModelScoreBatch(
            predictor.identity,
            {prediction.code: prediction for prediction in predictions},
            predictions,
            tuple(missing),
            score_scale=TOMORROW_PROBABILITY_SCORE_SCALE,
            score_model_id=predictor.identity if self.target is not None else TOMORROW_CLOSE_GAIN_TARGET,
            score_contract_hash=predictor.identity,
            prediction_target=self.target,
        )

    def _predict_probabilities(
        self,
        predictor: TomorrowProbabilityPredictor,
        rows: tuple[tuple[float, ...], ...],
        deadline: float | None,
    ) -> tuple[float, ...]:
        batch_size = self.plan.batch_size
        if batch_size is None:
            return predictor.predict_probabilities(rows) if rows else ()
        probabilities: list[float] = []
        for start in range(0, len(rows), batch_size):
            self._check_deadline(deadline)
            batch = rows[start:start + batch_size]
            predicted = predictor.predict_probabilities(batch)
            self._check_deadline(deadline)
            if len(predicted) != len(batch):
                raise ValueError("Tomorrow sequence model returned the wrong batch size")
            probabilities.extend(predicted)
        return tuple(probabilities)

    def _check_deadline(self, deadline: float | None) -> None:
        if deadline is not None and self.monotonic() >= deadline:
            raise ModelScoringDeadlineError(f"tomorrow_{self.plan.profile_id}_model_scoring_deadline")

    def status(self) -> ScoringHeadRuntimeStatus:
        identity = self.predictor.identity if self.predictor is not None else ""
        return ScoringHeadRuntimeStatus(
            is_enabled=self.predictor is not None,
            profile_id=self.plan.profile_id,
            model_id=identity if self.target is not None else TOMORROW_CLOSE_GAIN_TARGET,
            prediction_target=self.target,
            model_hash=identity,
            scoring_version=identity,
            activation_basis="trained_artifact",
            historical_status="historical_data_insufficient",
            historical_failure_reasons=(
                ("tomorrow_probability_model_unavailable",) if self.predictor is None else ("point_in_time_not_proven",)
            ),
            monitoring_mode="automatic_t1_outcome_settlement",
            automatic_model_update=False,
            loss_probability_status="not_modeled",
            score_scale=TOMORROW_PROBABILITY_SCORE_SCALE,
        )


def BuildQuoteFeatures(feature: FeatureSnapshot) -> tuple[float, ...] | None:
    quote = feature.quote
    return BuildTomorrowFeatures(
        quote.open_price, quote.high, quote.low, quote.price, quote.volume, BoardForSnapshot(feature)
    )
