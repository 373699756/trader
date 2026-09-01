"""Typed prediction/outcome ledger contracts for historical research."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.domain.research.h1_point_in_time import H1Strategy, canonical_hash

ResidualAnchor = Literal["11:20", "14:50"]
ResidualFilterState = Literal["passed", "observe_only", "not_ready"]
ResidualLabelStatus = Literal["label_pending", "matured"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^\d{6}$")
_IDENTITY = re.compile(r"^[a-z0-9_:-]{1,96}$")


@dataclass(frozen=True)
class ResidualJoinKey:
    strategy: H1Strategy
    trade_date: date
    anchor: ResidualAnchor
    code: str
    horizon: int

    def __post_init__(self) -> None:
        expected_anchor = "11:20" if self.strategy == "today" else "14:50"
        allowed_horizons = {1} if self.strategy != "d25" else {2, 3, 4, 5}
        if self.anchor != expected_anchor or self.horizon not in allowed_horizons:
            raise ValueError("historical residual identity does not match its strategy")
        if _CODE.fullmatch(self.code) is None:
            raise ValueError("historical residual code is invalid")


@dataclass(frozen=True)
class HistoricalPredictionRecord:
    key: ResidualJoinKey
    parent_split_hash: str
    feature_hash: str
    model_hash: str
    board: str
    industry: str
    market_state: str
    liquidity_state: str
    volatility_state: str
    predicted_net_excess_return: float | None
    score: float | None
    filter_state: ResidualFilterState
    selected: bool
    selection_reason: str
    schema_version: str = "historical_prediction_record_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_prediction_identity(self)
        _validate_prediction_value(self)
        if self.selected and self.filter_state != "passed":
            raise ValueError("historical prediction cannot select an ineligible row")
        if self.schema_version != "historical_prediction_record_v1" or self.production_authority:
            raise ValueError("historical prediction cannot authorize production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_prediction_identity(record: HistoricalPredictionRecord) -> None:
    for value, label in (
        (record.parent_split_hash, "parent split"),
        (record.feature_hash, "feature"),
        (record.model_hash, "model"),
    ):
        _hash(value, label)
    for value in (
        record.board,
        record.industry,
        record.market_state,
        record.liquidity_state,
        record.volatility_state,
        record.selection_reason,
    ):
        if _IDENTITY.fullmatch(value) is None:
            raise ValueError("historical prediction categorical identity is invalid")
    if record.filter_state not in {"passed", "observe_only", "not_ready"}:
        raise ValueError("historical prediction filter state is invalid")


def _validate_prediction_value(record: HistoricalPredictionRecord) -> None:
    predicted = record.predicted_net_excess_return
    if predicted is None:
        if record.score is not None or record.selected or record.selection_reason != "not_modeled":
            raise ValueError("not-modeled prediction must keep prediction and score null")
    elif not math.isfinite(predicted) or record.score is None or not math.isfinite(record.score):
        raise ValueError("historical prediction values must be finite together")
    elif not 0.0 <= record.score <= 100.0:
        raise ValueError("historical prediction score must be in [0, 100]")


@dataclass(frozen=True)
class HistoricalOutcomeRecord:
    key: ResidualJoinKey
    parent_split_hash: str
    gross_return: float | None
    benchmark_return: float | None
    round_trip_cost: float | None
    actual_net_excess_return: float | None
    mae_atr20: float | None
    severe_loss: bool | None
    label_status: ResidualLabelStatus
    schema_version: str = "historical_outcome_record_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.parent_split_hash, "parent split")
        values = (
            self.gross_return,
            self.benchmark_return,
            self.round_trip_cost,
            self.actual_net_excess_return,
            self.mae_atr20,
        )
        if self.label_status == "label_pending":
            if any(value is not None for value in values) or self.severe_loss is not None:
                raise ValueError("pending historical outcome values must remain null")
        elif self.label_status == "matured":
            if any(value is None or not math.isfinite(value) for value in values) or self.severe_loss is None:
                raise ValueError("matured historical outcome requires finite label values")
            assert self.gross_return is not None
            assert self.benchmark_return is not None
            assert self.round_trip_cost is not None
            assert self.actual_net_excess_return is not None
            expected = self.gross_return - self.benchmark_return - self.round_trip_cost
            if not math.isclose(expected, self.actual_net_excess_return, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("historical outcome net excess arithmetic is inconsistent")
            if self.round_trip_cost < 0.0 or self.mae_atr20 is None or self.mae_atr20 > 0.0:
                raise ValueError("historical outcome cost or signed adverse excursion is invalid")
            severe_threshold = -2.5 if self.key.strategy == "d25" else -1.5
            if self.severe_loss != (self.mae_atr20 <= severe_threshold):
                raise ValueError("historical outcome severe-loss label is inconsistent")
        else:
            raise ValueError("historical outcome label status is invalid")
        if self.schema_version != "historical_outcome_record_v1" or self.production_authority:
            raise ValueError("historical outcome cannot authorize production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class JoinedHistoricalResidual:
    prediction: HistoricalPredictionRecord
    outcome: HistoricalOutcomeRecord
    prediction_error: float
    schema_version: str = "historical_joined_residual_v1"
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.prediction.key != self.outcome.key:
            raise ValueError("historical residual identity does not match")
        if self.prediction.parent_split_hash != self.outcome.parent_split_hash:
            raise ValueError("historical residual parent split does not match")
        if (
            self.prediction.predicted_net_excess_return is None
            or self.outcome.label_status != "matured"
            or self.outcome.actual_net_excess_return is None
        ):
            raise ValueError("historical residual outcome is not matured")
        expected = self.outcome.actual_net_excess_return - self.prediction.predicted_net_excess_return
        if not math.isclose(self.prediction_error, expected, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("historical residual prediction error is inconsistent")
        if self.schema_version != "historical_joined_residual_v1" or self.production_authority:
            raise ValueError("historical residual cannot authorize production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalResidualSummary:
    parent_split_hash: str
    evaluated_trade_dates: int
    evaluated_rows: int
    selected_rows: int
    unselected_rows: int
    mean_error: float
    mean_absolute_error: float
    direction_hit_rate: float
    group_metrics: tuple[tuple[str, str, int, float, float], ...]
    schema_version: str = "historical_prediction_residual_summary_v1"
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.parent_split_hash, "parent split")
        if min(self.evaluated_trade_dates, self.evaluated_rows, self.selected_rows, self.unselected_rows) < 0:
            raise ValueError("historical residual summary counts are invalid")
        if self.selected_rows + self.unselected_rows != self.evaluated_rows:
            raise ValueError("historical residual summary population is inconsistent")
        if not all(
            math.isfinite(value) for value in (self.mean_error, self.mean_absolute_error, self.direction_hit_rate)
        ):
            raise ValueError("historical residual summary metrics must be finite")
        if not 0.0 <= self.direction_hit_rate <= 1.0:
            raise ValueError("historical residual direction hit rate is invalid")
        if self.terminal_holdout_opened or self.production_authority:
            raise ValueError("historical residual summary cannot open holdout or production")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def join_prediction_outcome(
    prediction: HistoricalPredictionRecord,
    outcome: HistoricalOutcomeRecord,
) -> JoinedHistoricalResidual:
    if prediction.key != outcome.key:
        raise ValueError("historical residual identity does not match")
    if prediction.parent_split_hash != outcome.parent_split_hash:
        raise ValueError("historical residual parent split does not match")
    if prediction.predicted_net_excess_return is None or outcome.actual_net_excess_return is None:
        raise ValueError("historical residual outcome is not matured or prediction is not modeled")
    return JoinedHistoricalResidual(
        prediction,
        outcome,
        outcome.actual_net_excess_return - prediction.predicted_net_excess_return,
    )


def summarize_residuals(rows: tuple[JoinedHistoricalResidual, ...]) -> HistoricalResidualSummary:
    if not rows:
        raise ValueError("historical residual summary requires matured rows")
    parents = {row.prediction.parent_split_hash for row in rows}
    if len(parents) != 1:
        raise ValueError("historical residual summary requires one parent split")
    ordered = tuple(sorted(rows, key=lambda row: (row.prediction.key.trade_date, row.prediction.key.code)))
    errors = tuple(row.prediction_error for row in ordered)
    hits = tuple(
        (row.prediction.predicted_net_excess_return or 0.0) * (row.outcome.actual_net_excess_return or 0.0) > 0.0
        for row in ordered
    )
    return HistoricalResidualSummary(
        parent_split_hash=next(iter(parents)),
        evaluated_trade_dates=len({row.prediction.key.trade_date for row in ordered}),
        evaluated_rows=len(ordered),
        selected_rows=sum(row.prediction.selected for row in ordered),
        unselected_rows=sum(not row.prediction.selected for row in ordered),
        mean_error=math.fsum(errors) / len(errors),
        mean_absolute_error=math.fsum(abs(value) for value in errors) / len(errors),
        direction_hit_rate=sum(hits) / len(hits),
        group_metrics=_group_metrics(ordered),
    )


def _group_metrics(rows: tuple[JoinedHistoricalResidual, ...]) -> tuple[tuple[str, str, int, float, float], ...]:
    groups: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        prediction = row.prediction
        dimensions = (
            ("market", prediction.market_state),
            ("board", prediction.board),
            ("liquidity", prediction.liquidity_state),
            ("volatility", prediction.volatility_state),
            ("selection", "top6" if prediction.selected else "unselected"),
        )
        for identity in dimensions:
            groups.setdefault(identity, []).append(row.prediction_error)
    return tuple(
        (dimension, key, len(values), math.fsum(values) / len(values), math.fsum(abs(v) for v in values) / len(values))
        for (dimension, key), values in sorted(groups.items())
    )


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"historical residual {label} identity must be SHA-256")


__all__ = [
    "HistoricalOutcomeRecord",
    "HistoricalPredictionRecord",
    "HistoricalResidualSummary",
    "JoinedHistoricalResidual",
    "ResidualAnchor",
    "ResidualFilterState",
    "ResidualJoinKey",
    "ResidualLabelStatus",
    "join_prediction_outcome",
    "summarize_residuals",
]
