"""Cross-sectional production scoring for one configured strategy head."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar, cast

from trader.application.cache import request_fingerprint
from trader.application.market_data.feature_computation import (
    FeatureComputationPlan,
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
    ScoringHeadRuntimeStatus,
)
from trader.application.runtime.schedule import shanghai_now
from trader.domain.market.factors import round_score
from trader.domain.market.feature_contracts import (
    FEATURE_SPEC_CATALOG,
    TOMORROW_MODEL_FEATURE_MANIFEST,
    V2_FEATURE_SPEC_CATALOG,
    V2_TOMORROW_MODEL_FEATURE_MANIFEST,
)
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.filtering.filters import board_for_snapshot
from trader.domain.recommendation.model_scoring import (
    ExposureContract,
    percentile_ranks,
    residualize_exposure,
)
from trader.domain.recommendation.models import Strategy

_AMOUNT_FIELD = "qfq_average_amount_20d"
_AMIHUD_FIELD = "qfq_amihud_20d"
_COST_RATE = 0.002
_DEFAULT_RETURN_HORIZONS = (1, 3, 5)
_DEFAULT_MOMENTUM_HORIZONS = (20, 40, 60)
_TStage = TypeVar("_TStage")


@dataclass(frozen=True)
class _RuntimeFeatureContract:
    computation_plan: FeatureComputationPlan
    return_horizons: tuple[int, ...]
    momentum_horizons: tuple[int, ...]
    market_state_horizons: tuple[int, ...]


@dataclass(frozen=True)
class _SharedFeatureMatrices:
    daily_returns: tuple[tuple[float | None, ...], ...]
    momenta: tuple[tuple[float | None, ...], ...]
    residuals: tuple[tuple[float, ...], ...]
    market_state: tuple[float | None, ...]


@dataclass(frozen=True)
class _RawRow:
    code: str
    board: str
    returns: tuple[float | None, ...]
    momentum: tuple[float | None, ...]
    amihud_20d: float
    average_amount_20d: float
    industry: str


@dataclass(frozen=True)
class _ModelRowContract:
    return_horizons: tuple[int, ...]
    momentum_horizons: tuple[int, ...]
    required_return_horizons: frozenset[int]
    required_momentum_horizons: frozenset[int]
    exposure_contract: ExposureContract
    industry_ids: frozenset[str]
    history_required_sessions: int


@dataclass(frozen=True)
class _ModelFeatureVectorContext:
    daily_returns: tuple[float | None, ...]
    momenta: tuple[float | None, ...]
    residuals: tuple[tuple[float, ...], ...]
    market_state: tuple[float | None, ...]
    return_horizons: tuple[int, ...]
    momentum_horizons: tuple[int, ...]
    market_state_horizons: tuple[int, ...]
    feature_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ModelComputationState:
    fact_revisions: tuple[FeatureFactRevision, ...]
    daily_returns: tuple[tuple[float | None, ...], ...]
    momenta: tuple[tuple[float | None, ...], ...]
    residuals: tuple[tuple[float, ...], ...]
    market_state: tuple[float | None, ...]
    inputs: tuple[ModelInput, ...]
    predictions: tuple[ModelPrediction, ...]
    cost_inputs: tuple[float, ...]
    missing_codes: tuple[str, ...]
    batch: ModelScoreBatch


class SharedModelFeatureCache:
    """Bounded exact-identity cache for cross-head feature calculations, never predictions."""

    def __init__(self, capacity: int = 3) -> None:
        if capacity < 1:
            raise ValueError("shared model feature cache capacity must be positive")
        self._capacity = capacity
        self._lock = threading.RLock()
        self._values: OrderedDict[
            tuple[tuple[FeatureFactRevision, ...], ExposureContract],
            _SharedFeatureMatrices,
        ] = OrderedDict()

    def read(
        self,
        revisions: tuple[FeatureFactRevision, ...],
        exposure_contract: ExposureContract,
    ) -> _SharedFeatureMatrices | None:
        key = (revisions, exposure_contract)
        with self._lock:
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value

    def publish(
        self,
        revisions: tuple[FeatureFactRevision, ...],
        exposure_contract: ExposureContract,
        value: _SharedFeatureMatrices,
    ) -> None:
        key = (revisions, exposure_contract)
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self._capacity:
                self._values.popitem(last=False)


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


class ProductionModelScoringService:
    def __init__(
        self,
        profile: LoadedScoringProfile,
        strategy: Strategy,
        *,
        monotonic: Callable[[], float] = time.perf_counter,
        shared_features: SharedModelFeatureCache | None = None,
    ) -> None:
        head = profile.heads.get(strategy)
        if head is None:
            raise ValueError(f"{strategy.value} production scoring requires its own head")
        predictor = cast(ModelPredictorPort, head.predictor)
        if profile.profile_id != predictor.profile_id:
            raise ValueError("scoring profile identity does not match its head")
        self._evidence = head.evidence
        if not predictor.model_id or len(predictor.model_hash) != 64 or predictor.profile_id not in {"v1", "v2", "v3"}:
            raise ValueError(f"{strategy.value} production model identity is invalid")
        try:
            self._runtime_features = _runtime_feature_contract(profile)
            self._feature_ids = tuple(predictor.feature_ids)
            if (
                not self._feature_ids
                or len(set(self._feature_ids)) != len(self._feature_ids)
                or any(
                    feature_id not in self._runtime_features.computation_plan.output_names
                    for feature_id in self._feature_ids
                )
            ):
                raise ValueError("predictor feature identity is invalid")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{strategy.value} production model identity is invalid") from exc
        self._strategy = strategy
        self._predictor = predictor
        self._required_return_horizons = frozenset(
            _feature_horizon(feature_id, "qfq_return_")
            for feature_id in self._feature_ids
            if feature_id.startswith("qfq_return_")
        )
        self._required_momentum_horizons = frozenset(
            _feature_horizon(feature_id, prefix)
            for feature_id in self._feature_ids
            for prefix in ("qfq_residual_momentum_", "market_qfq_momentum_")
            if feature_id.startswith(prefix)
        )
        self._history_required_sessions = max(
            61,
            max(self._required_momentum_horizons, default=60) + 1,
        )
        self._industry_ids = frozenset(predictor.industry_ids)
        self._exposure_contract = predictor.exposure_contract
        self._row_contract = _ModelRowContract(
            self._runtime_features.return_horizons,
            self._runtime_features.momentum_horizons,
            self._required_return_horizons,
            self._required_momentum_horizons,
            self._exposure_contract,
            self._industry_ids,
            self._history_required_sessions,
        )
        if self._exposure_contract.requires_industry and not self._industry_ids:
            raise ValueError(f"{strategy.value} production model industry coverage is missing")
        self._monotonic = monotonic
        self._shared_features = shared_features or SharedModelFeatureCache()
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
        return self._history_required_sessions

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool:
        row = _raw_row(feature, self._row_contract)
        return row is not None and (not self._exposure_contract.requires_industry or row.industry in self._industry_ids)

    def status(self) -> ScoringHeadRuntimeStatus:
        evidence = self._evidence
        return ScoringHeadRuntimeStatus(
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
    ) -> ModelScoreBatch:
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
    ) -> ModelScoreBatch:
        deadline.require_time("before_feature_computation")
        rows, missing_codes = _eligible_rows(
            features,
            self._row_contract,
        )
        codes = tuple(row.code for row in rows)
        if len(codes) != len(set(codes)):
            raise ValueError(f"{self._strategy.value} model scoring candidates must be unique")
        ordered_rows = tuple(rows)
        revisions = _fact_revisions(
            ordered_rows,
            self._runtime_features.momentum_horizons,
            self._runtime_features.computation_plan.required_fact_ids,
        )
        previous = self._state
        invalidation = affected_feature_stages(
            self._runtime_features.computation_plan,
            previous.fact_revisions if previous is not None else None,
            revisions,
        )
        affected = set(invalidation.affected_groups)
        shared = self._shared_features.read(revisions, self._exposure_contract)
        if shared is None:
            daily_returns = self._stage_value(
                "daily_return",
                affected,
                previous.daily_returns if previous is not None else None,
                lambda: tuple(row.returns for row in ordered_rows),
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
                lambda: _residualize_rows(
                    ordered_rows,
                    momenta,
                    self._runtime_features.momentum_horizons,
                    self._exposure_contract,
                ),
                deadline,
            )
            market_state = (
                self._stage_value(
                    "market_state",
                    affected,
                    previous.market_state if previous is not None else None,
                    lambda: _market_state_values(momenta, self._runtime_features.market_state_horizons),
                    deadline,
                )
                if self._runtime_features.market_state_horizons
                else ()
            )
            self._shared_features.publish(
                revisions,
                self._exposure_contract,
                _SharedFeatureMatrices(daily_returns, momenta, residuals, market_state),
            )
        else:
            daily_returns, momenta, residuals, market_state = (
                shared.daily_returns,
                shared.momenta,
                shared.residuals,
                shared.market_state,
            )
            affected.clear()
        computed_groups = () if shared is not None else invalidation.affected_groups
        inputs = tuple(
            ModelInput(
                row.code,
                tuple(
                    _model_feature_vector(
                        index,
                        _ModelFeatureVectorContext(
                            daily_returns[index],
                            momenta[index],
                            residuals,
                            market_state,
                            self._runtime_features.return_horizons,
                            self._runtime_features.momentum_horizons,
                            self._runtime_features.market_state_horizons,
                            self._feature_ids,
                        ),
                    )[feature_id]
                    for feature_id in self._feature_ids
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
            raise ValueError(f"{self._strategy.value} production model returned a mismatched prediction batch")
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
                computed_groups=computed_groups,
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
            market_state,
            inputs,
            predictions,
            cost_inputs,
            missing_codes,
            batch,
        )
        self._publish_computation_status(
            candidate_count=len(features),
            computed_groups=computed_groups,
            deadline=deadline,
        )
        return batch

    def _stage_value(
        self,
        group: str,
        affected: set[str],
        previous: _TStage | None,
        calculate: Callable[[], _TStage],
        deadline: _ScoringDeadline,
    ) -> _TStage:
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
        all_groups = tuple(stage.calculator_group for stage in self._runtime_features.computation_plan.stages)
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
        return ModelScoreBatch(model_version, {}, (), missing_codes)
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
    return ModelScoreBatch(model_version, diagnostics, predictions, missing_codes)


def _eligible_rows(
    features: tuple[FeatureSnapshot, ...],
    contract: _ModelRowContract,
) -> tuple[list[_RawRow], tuple[str, ...]]:
    rows: list[_RawRow] = []
    missing: list[str] = []
    for feature in sorted(features, key=lambda item: item.quote.code):
        row = _raw_row(feature, contract)
        if row is None or (
            contract.exposure_contract.requires_industry
            and (not row.industry or row.industry not in contract.industry_ids)
        ):
            missing.append(feature.quote.code)
        else:
            rows.append(row)
    return rows, tuple(missing)


def _residualize_rows(
    rows: tuple[_RawRow, ...],
    momenta: tuple[tuple[float | None, ...], ...],
    momentum_horizons: tuple[int, ...],
    exposure_contract: ExposureContract,
) -> tuple[tuple[float, ...], ...]:
    if not rows:
        return tuple(() for _ in momentum_horizons)
    residuals: list[tuple[float, ...]] = []
    for index in range(len(momentum_horizons)):
        values = tuple(row[index] for row in momenta)
        if any(value is None for value in values):
            residuals.append(())
            continue
        residuals.append(
            residualize_exposure(
                tuple(cast(float, value) for value in values),
                tuple(item.board for item in rows),
                tuple(item.average_amount_20d for item in rows),
                industries=tuple(item.industry for item in rows),
                contract=exposure_contract,
            )
        )
    return tuple(residuals)


def _fact_revisions(
    rows: tuple[_RawRow, ...],
    momentum_horizons: tuple[int, ...],
    required_fact_ids: tuple[str, ...],
) -> tuple[FeatureFactRevision, ...]:
    values: dict[str, object] = {
        "qfq_close_current": tuple((row.code, *row.returns) for row in rows),
        "market_cross_section": tuple(row.code for row in rows),
        "board_cross_section": tuple((row.code, row.board) for row in rows),
        "industry_cross_section": tuple((row.code, row.industry) for row in rows),
        "qfq_average_amount_20d": tuple((row.code, row.average_amount_20d) for row in rows),
    }
    return_by_horizon = {
        horizon: tuple((row.code, row.returns[index]) for row in rows)
        for index, horizon in enumerate(_DEFAULT_RETURN_HORIZONS)
    }
    momentum_by_horizon = {
        horizon: tuple((row.code, row.momentum[index]) for row in rows)
        for index, horizon in enumerate(momentum_horizons)
    }
    for horizon in (*_DEFAULT_RETURN_HORIZONS, *momentum_horizons):
        values[f"qfq_close_lag_{horizon}"] = tuple(
            (row.code, return_by_horizon.get(horizon), momentum_by_horizon.get(horizon)) for row in rows
        )
    return tuple(
        FeatureFactRevision(
            fact_id,
            request_fingerprint({"fact_id": fact_id, "values": _fact_value(values, fact_id)}),
        )
        for fact_id in required_fact_ids
    )


def _relative_prediction_scores(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(100.0 * rank for rank in percentile_ranks(values))


def _model_feature_vector(
    index: int,
    context: _ModelFeatureVectorContext,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for horizon, value in zip(context.return_horizons, context.daily_returns, strict=True):
        if value is not None:
            values[f"qfq_return_{horizon}d"] = value
    for horizon, value in zip(context.momentum_horizons, context.momenta, strict=True):
        if value is not None:
            values[f"qfq_momentum_{horizon}d_skip5"] = value
    for horizon, row_values in zip(context.momentum_horizons, context.residuals, strict=True):
        if row_values:
            values[f"qfq_residual_momentum_{horizon}d_skip5"] = row_values[index]
    for horizon, value in zip(context.market_state_horizons, context.market_state, strict=True):
        if value is not None:
            values[f"market_qfq_momentum_{horizon}d_skip5"] = value
    try:
        return {feature_id: values[feature_id] for feature_id in context.feature_ids}
    except KeyError as exc:
        raise ValueError("model feature vector contains a missing value") from exc


def _raw_row(
    feature: FeatureSnapshot,
    contract: _ModelRowContract,
) -> _RawRow | None:
    board = board_for_snapshot(feature)
    if board not in {Board.MAIN, Board.CHINEXT, Board.STAR}:
        return None
    return_values = tuple(
        _optional_finite(feature.values.get(f"qfq_return_{horizon}d")) for horizon in contract.return_horizons
    )
    momentum_values = tuple(
        _optional_finite(feature.values.get(f"qfq_momentum_{horizon}d_skip5")) for horizon in contract.momentum_horizons
    )
    required = (
        tuple(return_values[contract.return_horizons.index(horizon)] for horizon in contract.required_return_horizons)
        + tuple(
            momentum_values[contract.momentum_horizons.index(horizon)]
            for horizon in contract.required_momentum_horizons
        )
        + (
            _optional_finite(feature.values.get(_AMOUNT_FIELD)),
            _optional_finite(feature.values.get(_AMIHUD_FIELD)),
        )
    )
    if any(value is None for value in required):
        return None
    amount = cast(float, _optional_finite(feature.values.get(_AMOUNT_FIELD)))
    amihud = cast(float, _optional_finite(feature.values.get(_AMIHUD_FIELD)))
    if feature.history_days < contract.history_required_sessions or amount <= 0.0 or amihud < 0.0:
        return None
    model_industry = feature.model_industry
    if model_industry is not None and model_industry.effective_date > shanghai_now(feature.observed_at).date():
        model_industry = None
    industry = (
        model_industry.industry_id.strip()
        if contract.exposure_contract.requires_industry and model_industry is not None
        else ""
        if contract.exposure_contract.requires_industry
        else feature.quote.industry.strip()
    )
    return _RawRow(
        code=feature.quote.code,
        board=board.value,
        returns=return_values,
        momentum=momentum_values,
        amihud_20d=amihud,
        average_amount_20d=amount,
        industry=industry,
    )


def _optional_finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _runtime_feature_contract(profile: LoadedScoringProfile) -> _RuntimeFeatureContract:
    profile_feature_ids = tuple(
        feature_id
        for head in profile.heads.values()
        for feature_id in cast(ModelPredictorPort, head.predictor).feature_ids
    )
    uses_v2_long_horizon = profile.profile_id == "v2" and any(
        horizon in feature_id for feature_id in profile_feature_ids for horizon in ("120d", "250d")
    )
    momentum_horizons: tuple[int, ...]
    market_state_horizons: tuple[int, ...]
    if uses_v2_long_horizon:
        manifest = V2_TOMORROW_MODEL_FEATURE_MANIFEST
        catalog = V2_FEATURE_SPEC_CATALOG
        momentum_horizons = (20, 40, 60, 120, 250)
        market_state_horizons = (120, 250)
    else:
        manifest = TOMORROW_MODEL_FEATURE_MANIFEST
        catalog = FEATURE_SPEC_CATALOG
        momentum_horizons = _DEFAULT_MOMENTUM_HORIZONS
        market_state_horizons = ()
    return _RuntimeFeatureContract(
        build_feature_computation_plan(manifest, catalog),
        _DEFAULT_RETURN_HORIZONS,
        momentum_horizons,
        market_state_horizons,
    )


def _feature_horizon(feature_id: str, prefix: str) -> int:
    suffix = feature_id.removeprefix(prefix).removesuffix("d_skip5").removesuffix("d")
    if not suffix.isdigit() or int(suffix) < 1:
        raise ValueError("model feature horizon is invalid")
    return int(suffix)


def _fact_value(values: dict[str, object], fact_id: str) -> object:
    try:
        return values[fact_id]
    except KeyError as exc:
        raise ValueError(f"model feature fact is unsupported: {fact_id}") from exc


def _market_state_values(
    momenta: tuple[tuple[float | None, ...], ...],
    market_state_horizons: tuple[int, ...],
) -> tuple[float | None, ...]:
    if not market_state_horizons:
        return ()
    result: list[float | None] = []
    for index in range(len(market_state_horizons)):
        values = tuple(row[index] for row in momenta)
        result.append(
            math.fsum(cast(float, value) for value in values) / len(values) if values and None not in values else None
        )
    return tuple(result)


__all__ = [
    "ProductionModelScoringService",
    "SharedModelFeatureCache",
]
