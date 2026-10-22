"""Rank a fixed complete population with the isolated V1 model capability."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import cast

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.ports.loaded_profile import (
    LearnedRankPredictorPort,
    LoadedScoringProfile,
    ModelDiagnostics,
    ModelInput,
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringDeadlineError,
    ScoringHeadRuntimeStatus,
)
from trader.recommendation.domain.market.factors import PercentileReference, RoundScore
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import LEARNED_RANK_SCORE_SCALE
from trader.recommendation.domain.scoring.utility_scoring import PercentileRanks
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy


class LearnedRankScoringService:
    """Keep rank percentiles independent of the existing local action/risk score."""

    history_required_sessions = 251

    def __init__(
        self,
        profile: LoadedScoringProfile,
        strategy: Strategy,
        *,
        cost_policy: ExecutionCostPolicy,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if profile.profile_id != "v1" or strategy not in profile.heads:
            raise ValueError("learned rank requires its own complete profile head")
        self._head = profile.heads[strategy]
        if self._head.evidence.production_authority:
            raise ValueError("learned rank production authority has not been validated")
        self._predictor = cast(LearnedRankPredictorPort, self._head.predictor)
        self._strategy = strategy
        self._cost_policy = cost_policy
        self._monotonic = monotonic
        self._contract_hash = ContentHash(
            (
                "completed_daily_251",
                self._predictor.model_hash,
                self._predictor.feature_ids,
                LEARNED_RANK_SCORE_SCALE,
                cost_policy,
            )
        )

    @property
    def model_version(self) -> str:
        return f"{self._predictor.model_id}:{self._predictor.model_hash}"

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool:
        return (
            feature.history_days >= self.history_required_sessions
            and all(
                isinstance(value := feature.values.get(name), (float, int))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for name in self._predictor.feature_ids
            )
            and feature.optional_value("qfq_amihud_20d") is not None
        )

    def score(
        self, features: Sequence[FeatureSnapshot], *, context: ModelScoringContext | None = None
    ) -> ModelScoreBatch:
        deadline = (
            self._monotonic() + context.time_budget_seconds
            if context is not None and context.time_budget_seconds is not None
            else None
        )
        if deadline is not None and self._monotonic() >= deadline:
            raise ModelScoringDeadlineError("learned_rank_deadline_exceeded")
        source = (
            context.population_features if context is not None and context.population_features is not None else features
        )
        population = tuple(
            sorted((item for item in source if self.is_input_eligible(item)), key=lambda item: item.quote.code)
        )
        population_codes = tuple(item.quote.code for item in population)
        requested_codes = tuple(item.quote.code for item in features)
        if len(set(population_codes)) != len(population_codes) or len(set(requested_codes)) != len(requested_codes):
            raise ValueError("learned rank population must contain unique codes")
        if not set(requested_codes) <= {item.quote.code for item in source}:
            raise ValueError("learned rank requests must belong to the fixed population")
        inputs = tuple(
            ModelInput(
                item.quote.code, tuple(float(cast(float, item.values[name])) for name in self._predictor.feature_ids)
            )
            for item in population
        )
        predictions = self._predictor.predict(inputs) if inputs else ()
        if deadline is not None and self._monotonic() >= deadline:
            raise ModelScoringDeadlineError("learned_rank_deadline_exceeded")
        if tuple(item.code for item in predictions) != population_codes or any(
            item.rank_raw is None for item in predictions
        ):
            raise ValueError("learned rank predictor returned an incomplete population")
        ranks = PercentileRanks(tuple(cast(float, item.rank_raw) for item in predictions))
        references = {
            board: PercentileReference(
                tuple(
                    cast(float, item.optional_value("qfq_amihud_20d"))
                    for item in population
                    if item.quote.board is board
                ),
                0.0,
                1.0,
            )
            for board in {item.quote.board for item in population}
        }
        diagnostics: dict[str, ModelDiagnostics] = {}
        for feature, prediction, rank in zip(population, predictions, ranks, strict=True):
            cost = self._cost_policy.estimate(
                references[feature.quote.board].rank(cast(float, feature.optional_value("qfq_amihud_20d")))
            )
            diagnostics[prediction.code] = ModelDiagnostics(
                RoundScore(100.0 * rank),
                prediction.predicted_excess_return * 100.0,
                cost * 100.0,
                (prediction.predicted_excess_return - cost) * 100.0,
            )
        requested = frozenset(requested_codes)
        selected = tuple(item for item in predictions if item.code in requested)
        return ModelScoreBatch(
            self.model_version,
            {item.code: diagnostics[item.code] for item in selected},
            selected,
            tuple(sorted(requested - set(population_codes))),
            cost_reference_version="learned-rank-cost:"
            + ContentHash(
                (
                    population_codes,
                    tuple((item.quote.board.value, item.optional_value("qfq_amihud_20d")) for item in population),
                )
            ),
            score_scale=LEARNED_RANK_SCORE_SCALE,
            score_model_id=self._predictor.model_id,
            score_contract_hash=self._contract_hash,
            production_authority=False,
            rank_population_version="learned-rank-population:"
            + ContentHash(
                (
                    self.model_version,
                    context.population_identity if context else "",
                    inputs,
                    tuple(item.rank_raw for item in predictions),
                )
            ),
        )

    def status(self) -> ScoringHeadRuntimeStatus:
        evidence = self._head.evidence
        return ScoringHeadRuntimeStatus(
            active=True,
            profile_id="v1",
            model_id=self._predictor.model_id,
            model_hash=self._predictor.model_hash,
            scoring_version=self.model_version,
            activation_basis=evidence.activation_basis,
            historical_status=evidence.historical_status,
            historical_failure_reasons=evidence.historical_failure_reasons,
            monitoring_mode=evidence.monitoring_mode,
            automatic_model_update=False,
            loss_probability_status="not_modeled",
            training_anchor=evidence.training_anchor,
            runtime_anchor=evidence.runtime_anchor,
            point_in_time_parity=evidence.point_in_time_parity,
            score_scale=LEARNED_RANK_SCORE_SCALE,
            production_authority=False,
            training_label_cutoff=evidence.training_label_cutoff,
        )


__all__ = ["LearnedRankScoringService"]
