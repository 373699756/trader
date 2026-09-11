"""Cross-sectional production scoring for the configured packaged Tomorrow model."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from trader.application.cache import request_fingerprint
from trader.application.market_data.feature_computation import (
    FeatureFactRevision,
    affected_feature_stages,
    build_feature_computation_plan,
)
from trader.application.ports.model_scoring import (
    LoadedScoringProfile,
    ModelComputationStageStatus,
    ModelComputationStatus,
    ModelDiagnostics,
    ModelInput,
    ModelPrediction,
    ModelPredictorPort,
    ModelScoreBatch,
    ModelScoringContext,
    ModelScoringDeadlineError,
    ScoringProfileRuntimeStatus,
)
from trader.domain.market.factors import round_score
from trader.domain.market.feature_contracts import (
    TOMORROW_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    FeatureValue,
)
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.filtering.filters import board_for_snapshot
from trader.domain.recommendation.model_scoring import (
    ExposureContract,
    percentile_ranks,
    residualize_exposure,
)
from trader.domain.recommendation.models import Strategy

_ALPHA_FIELDS = TOMORROW_RAW_ALPHA_FEATURE_MANIFEST.names
_AMOUNT_FIELD = "qfq_average_amount_20d"
_AMIHUD_FIELD = "qfq_amihud_20d"
_COST_RATE = 0.002
_HISTORY_REQUIRED_SESSIONS = 61
_MODEL_COMPUTATION_PLAN = build_feature_computation_plan(TOMORROW_MODEL_FEATURE_MANIFEST)
_MODEL_FEATURE_IDS = _MODEL_COMPUTATION_PLAN.output_names
TomorrowModelDiagnostics = ModelDiagnostics
TomorrowModelScoreBatch = ModelScoreBatch


@dataclass(frozen=True)
class _RawRow:
    code: str
    board: str
    return_1d: float
    return_3d: float
    return_5d: float
    momentum: tuple[float, float, float]
    amihud_20d: float
    average_amount_20d: float
    industry: str


@dataclass(frozen=True)
class _ModelComputationState:
    fact_revisions: tuple[FeatureFactRevision, ...]
    daily_returns: tuple[tuple[float, ...], ...]
    momenta: tuple[tuple[float, ...], ...]
    residuals: tuple[tuple[float, ...], ...]
    inputs: tuple[ModelInput, ...]
    predictions: tuple[ModelPrediction, ...]
    cost_inputs: tuple[float, ...]
    missing_codes: tuple[str, ...]
    batch: ModelScoreBatch


class _ScoringDeadline:
    def __init__(
        self,
        context: ModelScoringContext,
        monotonic: Callable[[], float],
    ) -> None:
        self._context = context
        self._monotonic = monotonic
        self._started_at = monotonic()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._monotonic() - self._started_at)

    @property
    def decision_age_ms(self) -> float:
        return (self._context.input_age_seconds + self.elapsed_seconds) * 1000.0

    def require_time(self, reason: str) -> None:
        budget = self._context.time_budget_seconds
        if budget is not None and self.elapsed_seconds >= max(0.0, budget):
            raise ModelScoringDeadlineError(reason)


class TomorrowProductionModelScoringService:
    def __init__(
        self,
        profile: LoadedScoringProfile,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        if len(profile.heads) != 1 or profile.heads[0].strategy is not Strategy.TOMORROW:
            raise ValueError("Tomorrow production scoring requires exactly one Tomorrow head")
        predictor = cast(ModelPredictorPort, profile.heads[0].predictor)
        if (
            profile.identity.profile_id != predictor.profile_id
            or profile.identity.model_id != predictor.model_id
            or profile.identity.model_hash != predictor.model_hash
        ):
            raise ValueError("scoring profile identity does not match its head")
        self._evidence = profile.evidence
        if (
            not predictor.model_id
            or len(predictor.model_hash) != 64
            or predictor.profile_id not in {"v1", "v2", "v3"}
            or not predictor.feature_ids
            or len(set(predictor.feature_ids)) != len(predictor.feature_ids)
            or any(feature_id not in _MODEL_FEATURE_IDS for feature_id in predictor.feature_ids)
        ):
            raise ValueError("Tomorrow production model identity is invalid")
        self._predictor = predictor
        self._feature_positions = tuple(_MODEL_FEATURE_IDS.index(item) for item in predictor.feature_ids)
        self._requires_reversal = any(position < 3 for position in self._feature_positions)
        self._industry_ids = frozenset(predictor.industry_ids)
        self._exposure_contract = predictor.exposure_contract
        if self._exposure_contract.requires_industry and not self._industry_ids:
            raise ValueError("Tomorrow production model industry coverage is missing")
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._state: _ModelComputationState | None = None
        self._request_count = 0
        self._cache_hit_count = 0
        self._predictor_batch_count = 0
        self._stage_metrics: dict[str, tuple[int, float, float]] = {}
        self._computation_status = ModelComputationStatus()

    @property
    def model_version(self) -> str:
        return f"{self._predictor.model_id}:{self._predictor.model_hash}"

    @property
    def history_required_sessions(self) -> int:
        return _HISTORY_REQUIRED_SESSIONS

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool:
        row = _raw_row(feature, require_reversal=self._requires_reversal)
        return row is not None and (not self._exposure_contract.requires_industry or row.industry in self._industry_ids)

    def status(self) -> ScoringProfileRuntimeStatus:
        evidence = self._evidence
        return ScoringProfileRuntimeStatus(
            active=True,
            profile_id=self._predictor.profile_id,
            model_id=self._predictor.model_id,
            model_hash=self._predictor.model_hash,
            scoring_version=self.model_version,
            activation_basis=evidence.activation_basis,
            historical_status=evidence.historical_status,
            historical_failure_reasons=evidence.historical_failure_reasons,
            monitoring_mode=evidence.monitoring_mode,
            automatic_model_update=evidence.automatic_model_update,
            loss_probability_status=evidence.loss_probability_status,
            computation=self.computation_status(),
            training_anchor=evidence.training_anchor,
            runtime_anchor=evidence.runtime_anchor,
            point_in_time_parity=evidence.point_in_time_parity,
        )

    def computation_status(self) -> ModelComputationStatus:
        with self._lock:
            return self._computation_status

    def score(
        self,
        features: Sequence[FeatureSnapshot],
        *,
        context: ModelScoringContext | None = None,
    ) -> TomorrowModelScoreBatch:
        deadline = _ScoringDeadline(context or ModelScoringContext(), self._monotonic)
        with self._lock:
            self._request_count += 1
            try:
                return self._score_locked(tuple(features), deadline)
            except ModelScoringDeadlineError as exc:
                self._publish_computation_status(
                    candidate_count=len(features),
                    computed_groups=(),
                    deadline=deadline,
                    abandon_reason=str(exc),
                )
                raise

    def _score_locked(
        self,
        features: tuple[FeatureSnapshot, ...],
        deadline: _ScoringDeadline,
    ) -> TomorrowModelScoreBatch:
        deadline.require_time("before_feature_computation")
        rows: list[_RawRow] = []
        missing: list[str] = []
        for feature in sorted(features, key=lambda item: item.quote.code):
            row = _raw_row(feature, require_reversal=self._requires_reversal)
            if row is None:
                missing.append(feature.quote.code)
            elif self._exposure_contract.requires_industry and (
                not row.industry or row.industry not in self._industry_ids
            ):
                missing.append(feature.quote.code)
            else:
                rows.append(row)
        codes = tuple(row.code for row in rows)
        if len(codes) != len(set(codes)):
            raise ValueError("Tomorrow model scoring candidates must be unique")
        ordered_rows = tuple(rows)
        missing_codes = tuple(missing)
        revisions = _fact_revisions(ordered_rows)
        previous = self._state
        invalidation = affected_feature_stages(
            _MODEL_COMPUTATION_PLAN,
            previous.fact_revisions if previous is not None else None,
            revisions,
        )
        affected = set(invalidation.affected_groups)
        daily_returns = self._stage_value(
            "daily_return",
            affected,
            previous.daily_returns if previous is not None else None,
            lambda: tuple((row.return_1d, row.return_3d, row.return_5d) for row in ordered_rows),
            deadline,
        )
        momenta = self._stage_value(
            "skip_recent_momentum",
            affected,
            previous.momenta if previous is not None else None,
            lambda: tuple(row.momentum for row in ordered_rows),
            deadline,
        )
        residuals = self._stage_value(
            "cross_section_residual",
            affected,
            previous.residuals if previous is not None else None,
            lambda: _residualize_rows(ordered_rows, momenta, self._exposure_contract),
            deadline,
        )
        inputs = tuple(
            ModelInput(
                row.code,
                tuple(
                    _model_feature_vector(daily_returns[index], residuals, index)[position]
                    for position in self._feature_positions
                ),
                row.industry,
            )
            for index, row in enumerate(ordered_rows)
        )
        predictions = previous.predictions if previous is not None and inputs == previous.inputs else None
        if not inputs:
            predictions = ()
        elif predictions is None:
            deadline.require_time("before_batch_prediction")
            started_at = self._monotonic()
            predictions = self._predictor.predict(inputs)
            self._record_stage("batch_prediction", started_at)
            self._predictor_batch_count += 1
            deadline.require_time("completed_after_batch_prediction_deadline")
        if tuple(item.code for item in predictions) != tuple(item.code for item in inputs):
            raise ValueError("Tomorrow production model returned a mismatched prediction batch")
        cost_inputs = tuple(row.amihud_20d for row in ordered_rows)
        if (
            previous is not None
            and inputs == previous.inputs
            and cost_inputs == previous.cost_inputs
            and missing_codes == previous.missing_codes
        ):
            self._cache_hit_count += 1
            self._publish_computation_status(
                candidate_count=len(features),
                computed_groups=invalidation.affected_groups,
                deadline=deadline,
            )
            return previous.batch
        deadline.require_time("before_score_projection")
        batch = _score_predictions(self.model_version, predictions, cost_inputs, missing_codes)
        deadline.require_time("completed_after_score_projection_deadline")
        self._state = _ModelComputationState(
            revisions,
            daily_returns,
            momenta,
            residuals,
            inputs,
            predictions,
            cost_inputs,
            missing_codes,
            batch,
        )
        self._publish_computation_status(
            candidate_count=len(features),
            computed_groups=invalidation.affected_groups,
            deadline=deadline,
        )
        return batch

    def _stage_value(
        self,
        group: str,
        affected: set[str],
        previous: tuple[tuple[float, ...], ...] | None,
        calculate: Callable[[], tuple[tuple[float, ...], ...]],
        deadline: _ScoringDeadline,
    ) -> tuple[tuple[float, ...], ...]:
        if group not in affected and previous is not None:
            return previous
        deadline.require_time(f"before_{group}")
        started_at = self._monotonic()
        result = calculate()
        self._record_stage(group, started_at)
        deadline.require_time(f"after_{group}")
        return result

    def _record_stage(self, stage: str, started_at: float) -> None:
        duration_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
        count, _last, total = self._stage_metrics.get(stage, (0, 0.0, 0.0))
        self._stage_metrics[stage] = (count + 1, duration_ms, total + duration_ms)

    def _publish_computation_status(
        self,
        *,
        candidate_count: int,
        computed_groups: tuple[str, ...],
        deadline: _ScoringDeadline,
        abandon_reason: str | None = None,
    ) -> None:
        all_groups = tuple(stage.calculator_group for stage in _MODEL_COMPUTATION_PLAN.stages)
        computed = set(computed_groups)
        self._computation_status = ModelComputationStatus(
            candidate_count=candidate_count,
            request_count=self._request_count,
            cache_hit_count=self._cache_hit_count,
            predictor_batch_count=self._predictor_batch_count,
            computed_groups=computed_groups,
            reused_groups=tuple(group for group in all_groups if group not in computed),
            stage_durations=tuple(
                ModelComputationStageStatus(stage, count, last, total)
                for stage, (count, last, total) in sorted(self._stage_metrics.items())
            ),
            decision_age_ms=deadline.decision_age_ms,
            deadline_abandon_reason=abandon_reason,
        )


def _score_predictions(
    model_version: str,
    predictions: tuple[ModelPrediction, ...],
    cost_inputs: tuple[float, ...],
    missing_codes: tuple[str, ...],
) -> ModelScoreBatch:
    if not predictions:
        return TomorrowModelScoreBatch(model_version, {}, (), missing_codes)
    amihud_ranks = percentile_ranks(cost_inputs)
    costs = tuple(_COST_RATE * (1.0 + rank) for rank in amihud_ranks)
    utilities = tuple(
        prediction.predicted_excess_return - cost for prediction, cost in zip(predictions, costs, strict=True)
    )
    prediction_scores = _relative_prediction_scores(
        tuple(prediction.predicted_excess_return for prediction in predictions)
    )
    diagnostics: dict[str, ModelDiagnostics] = {}
    for prediction, cost, utility, score in zip(
        predictions,
        costs,
        utilities,
        prediction_scores,
        strict=True,
    ):
        predicted_pct = prediction.predicted_excess_return * 100.0
        cost_pct = cost * 100.0
        net_pct = utility * 100.0
        disagreement_pct = prediction.model_disagreement * 100.0
        diagnostics[prediction.code] = ModelDiagnostics(
            signal_score=round_score(score),
            predicted_excess_return_pct=predicted_pct,
            estimated_cost_pct=cost_pct,
            predicted_net_excess_pct=net_pct,
            model_disagreement_pct=disagreement_pct,
        )
    return TomorrowModelScoreBatch(model_version, diagnostics, predictions, missing_codes)


def _residualize_rows(
    rows: tuple[_RawRow, ...],
    momenta: tuple[tuple[float, ...], ...],
    exposure_contract: ExposureContract,
) -> tuple[tuple[float, ...], ...]:
    if not rows:
        return ((), (), ())
    return tuple(
        residualize_exposure(
            tuple(row[index] for row in momenta),
            tuple(item.board for item in rows),
            tuple(item.average_amount_20d for item in rows),
            industries=tuple(item.industry for item in rows),
            contract=exposure_contract,
        )
        for index in range(3)
    )


def _fact_revisions(rows: tuple[_RawRow, ...]) -> tuple[FeatureFactRevision, ...]:
    values: dict[str, object] = {
        "qfq_close_current": tuple((row.code, row.return_1d, row.return_3d, row.return_5d) for row in rows),
        "qfq_close_lag_1": tuple((row.code, row.return_1d) for row in rows),
        "qfq_close_lag_3": tuple((row.code, row.return_3d) for row in rows),
        "qfq_close_lag_5": tuple((row.code, row.return_5d) for row in rows),
        "qfq_close_lag_20": tuple((row.code, row.momentum[0]) for row in rows),
        "qfq_close_lag_40": tuple((row.code, row.momentum[1]) for row in rows),
        "qfq_close_lag_60": tuple((row.code, row.momentum[2]) for row in rows),
        "market_cross_section": tuple(row.code for row in rows),
        "board_cross_section": tuple((row.code, row.board) for row in rows),
        "industry_cross_section": tuple((row.code, row.industry) for row in rows),
        "qfq_average_amount_20d": tuple((row.code, row.average_amount_20d) for row in rows),
    }
    return tuple(
        FeatureFactRevision(fact_id, request_fingerprint({"fact_id": fact_id, "values": values[fact_id]}))
        for fact_id in _MODEL_COMPUTATION_PLAN.required_fact_ids
    )


def _relative_prediction_scores(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(100.0 * rank for rank in percentile_ranks(values))


def _model_feature_vector(
    daily_returns: tuple[float, ...],
    residuals: tuple[tuple[float, ...], ...],
    index: int,
) -> tuple[float, ...]:
    raw = (*daily_returns, residuals[0][index], residuals[1][index], residuals[2][index])
    return TOMORROW_MODEL_FEATURE_MANIFEST.bind(
        tuple(
            FeatureValue(feature_id, value)
            for feature_id, value in zip(TOMORROW_MODEL_FEATURE_MANIFEST.feature_ids, raw, strict=True)
        )
    ).require_complete()


def _raw_row(feature: FeatureSnapshot, *, require_reversal: bool) -> _RawRow | None:
    board = board_for_snapshot(feature)
    if board not in {Board.MAIN, Board.CHINEXT, Board.STAR}:
        return None
    values = tuple(feature.values.get(name) for name in (*_ALPHA_FIELDS, _AMOUNT_FIELD, _AMIHUD_FIELD))
    required = (*values[3:6], *values[6:])
    if require_reversal:
        required = (*values[:3], *required)
    if any(value is None or not math.isfinite(value) for value in required):
        return None
    numeric = tuple(float(value) if value is not None else 0.0 for value in values)
    amount = numeric[6]
    amihud = numeric[7]
    if feature.history_days < _HISTORY_REQUIRED_SESSIONS or amount <= 0.0 or amihud < 0.0:
        return None
    return _RawRow(
        code=feature.quote.code,
        board=board.value,
        return_1d=numeric[0],
        return_3d=numeric[1],
        return_5d=numeric[2],
        momentum=(numeric[3], numeric[4], numeric[5]),
        amihud_20d=amihud,
        average_amount_20d=amount,
        industry=feature.quote.industry.strip(),
    )


__all__ = [
    "TomorrowModelDiagnostics",
    "TomorrowModelScoreBatch",
    "TomorrowProductionModelScoringService",
]
