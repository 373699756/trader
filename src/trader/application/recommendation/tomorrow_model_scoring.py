"""Cross-sectional production scoring for the configured packaged Tomorrow model."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from trader.application.ports.model_scoring import ModelDiagnostics, ModelScoreBatch
from trader.application.ports.tomorrow_model import (
    TomorrowModelInput,
    TomorrowModelPredictorPort,
    TomorrowModelRuntimeStatus,
)
from trader.domain.market.factors import clamp, round_score
from trader.domain.market.models import Board, FeatureSnapshot
from trader.domain.recommendation.filtering.filters import board_for_snapshot
from trader.domain.recommendation.model_scoring import (
    percentile_ranks,
    positive_utility_scores,
    residualize_exposure,
)
from trader.domain.recommendation.strategies.composition import LocalScoreResult

_ALPHA_FIELDS = (
    "p2_return_1d",
    "p2_return_3d",
    "p2_return_5d",
    "p2_momentum_20d_skip5",
    "p2_momentum_40d_skip5",
    "p2_momentum_60d_skip5",
)
_AMOUNT_FIELD = "p2_average_amount_20d"
_AMIHUD_FIELD = "p2_amihud_20d"
_COST_RATE = 0.002
_HISTORY_REQUIRED_SESSIONS = 61
_MODEL_FEATURE_IDS = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
_HistoricalStatus = Literal["historical_rejected", "historical_unavailable", "historical_validated"]
_ActivationBasis = Literal["manual_user_override", "trained_artifact"]
_HISTORICAL_EVIDENCE: Mapping[str, tuple[_HistoricalStatus, tuple[str, ...], _ActivationBasis]] = {
    "v1": (
        "historical_unavailable",
        (
            "original_five_candidate_research_artifact_unavailable",
            "manual_daily_proxy_not_original_research_evidence",
        ),
        "manual_user_override",
    ),
    "v2": (
        "historical_rejected",
        ("quintile_spread_not_positive", "severe_loss_rate_worse", "turnover_limit"),
        "manual_user_override",
    ),
    "v3": ("historical_validated", (), "trained_artifact"),
}


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


class TomorrowProductionModelScoringService:
    def __init__(self, predictor: TomorrowModelPredictorPort) -> None:
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
        self._industry_ids = frozenset(getattr(predictor, "industry_ids", ()))

    @property
    def model_version(self) -> str:
        return f"{self._predictor.model_id}:{self._predictor.model_hash}"

    @property
    def history_required_sessions(self) -> int:
        return _HISTORY_REQUIRED_SESSIONS

    def is_input_eligible(self, feature: FeatureSnapshot) -> bool:
        return _raw_row(feature, require_reversal=self._requires_reversal) is not None

    def status(self) -> TomorrowModelRuntimeStatus:
        historical_status, historical_failure_reasons, activation_basis = _HISTORICAL_EVIDENCE[
            self._predictor.profile_id
        ]
        return TomorrowModelRuntimeStatus(
            active=True,
            profile_id=self._predictor.profile_id,
            model_id=self._predictor.model_id,
            model_hash=self._predictor.model_hash,
            scoring_version=self.model_version,
            activation_basis=activation_basis,
            historical_status=historical_status,
            historical_failure_reasons=historical_failure_reasons,
            monitoring_mode="automatic_t1_outcome_settlement",
            automatic_model_update=False,
            loss_probability_status="not_modeled",
        )

    def score(self, features: Sequence[FeatureSnapshot]) -> TomorrowModelScoreBatch:
        rows: list[_RawRow] = []
        missing: list[str] = []
        for feature in sorted(features, key=lambda item: item.quote.code):
            row = _raw_row(feature, require_reversal=self._requires_reversal)
            if row is None:
                missing.append(feature.quote.code)
            elif self._industry_ids and row.industry not in self._industry_ids:
                missing.append(feature.quote.code)
            else:
                rows.append(row)
        if not rows:
            return TomorrowModelScoreBatch(self.model_version, {}, {}, (), tuple(missing))
        residuals = tuple(
            residualize_exposure(
                tuple(row.momentum[index] for row in rows),
                tuple(row.board for row in rows),
                tuple(row.average_amount_20d for row in rows),
            )
            for index in range(3)
        )
        inputs = tuple(
            TomorrowModelInput(
                row.code,
                tuple(
                    (
                        row.return_1d,
                        row.return_3d,
                        row.return_5d,
                        residuals[0][index],
                        residuals[1][index],
                        residuals[2][index],
                    )[position]
                    for position in self._feature_positions
                ),
                row.industry,
            )
            for index, row in enumerate(rows)
        )
        predictions = self._predictor.predict(inputs)
        if tuple(item.code for item in predictions) != tuple(item.code for item in inputs):
            raise ValueError("Tomorrow production model returned a mismatched prediction batch")
        amihud_ranks = percentile_ranks(tuple(row.amihud_20d for row in rows))
        costs = tuple(_COST_RATE * (1.0 + rank) for rank in amihud_ranks)
        utilities = tuple(
            prediction.predicted_excess_return - cost for prediction, cost in zip(predictions, costs, strict=True)
        )
        utility_scores = positive_utility_scores(utilities)
        scores: dict[str, LocalScoreResult] = {}
        diagnostics: dict[str, ModelDiagnostics] = {}
        for prediction, cost, utility, score in zip(
            predictions,
            costs,
            utilities,
            utility_scores,
            strict=True,
        ):
            predicted_pct = prediction.predicted_excess_return * 100.0
            cost_pct = cost * 100.0
            net_pct = utility * 100.0
            disagreement_pct = prediction.model_disagreement * 100.0
            components = {
                "model_net_utility_rank": round_score(score),
                "model_confidence": round_score(clamp(100.0 / (1.0 + 100.0 * prediction.model_disagreement))),
            }
            scores[prediction.code] = LocalScoreResult(components, round_score(score))
            diagnostics[prediction.code] = ModelDiagnostics(
                predicted_pct,
                cost_pct,
                net_pct,
                disagreement_pct,
            )
        return TomorrowModelScoreBatch(
            self.model_version,
            scores,
            diagnostics,
            predictions,
            tuple(missing),
        )


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
