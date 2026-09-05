from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.shadow_model_models import ShadowLabeledDay, ShadowSettlementLabel
from trader.application.research.shadow_model_ports import ShadowFitRequest, ShadowFitResult
from trader.application.research.shadow_models import ScoreTomorrowShadowModels
from trader.application.research.tomorrow_feature_models import TomorrowPointInTimeFeatureBatch
from trader.domain.research.historical import CostSettlementBasis
from trader.domain.research.tomorrow_features import (
    TOMORROW_FEATURE_NAMES,
    TomorrowFeatureValue,
    TomorrowStockFeatures,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
START = date(2026, 1, 5)
CODES = ("300001", "600001", "600002", "688001")


@dataclass
class _RecordingTrainer:
    model_family: str
    requests: list[ShadowFitRequest] = field(default_factory=list)

    def fit_predict(self, request: ShadowFitRequest) -> ShadowFitResult:
        self.requests.append(request)
        scale = 0.7 if self.model_family == "linear" else 0.9

        def predict(rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
            raw = tuple(scale * sum(row) / max(1, len(row)) for row in rows)
            if request.objective == "severe_loss":
                return tuple(1.0 / (1.0 + pow(2.718281828459045, -value)) for value in raw)
            return raw

        identity = hashlib.sha256(
            repr((self.model_family, request.objective, request.train_x, request.train_y, request.seed)).encode()
        ).hexdigest()
        return ShadowFitResult(
            model_family=self.model_family,
            model_hash=identity,
            calibration_predictions=predict(request.calibration_x),
            prediction_predictions=predict(request.prediction_x),
        )


def test_shadow_models_use_walk_forward_embargo_same_data_and_complete_predictions() -> None:
    linear = _RecordingTrainer("linear")
    lightgbm = _RecordingTrainer("lightgbm")
    service = ScoreTomorrowShadowModels((linear, lightgbm))
    days = tuple(day for index in range(66) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))

    report = service.build(days)

    assert report.schema_version == "score_tomorrow_shadow_report"
    assert report.feature_version == "score_tomorrow_point_in_time_features"
    assert report.random_seed == 20260828
    assert report.cost_bps == 20
    assert report.production_authority is False
    assert len(report.content_hash) == 64
    assert {fold.window_mode for fold in report.folds} == {"expanding", "rolling"}
    assert {fold.horizon for fold in report.folds} == {"tomorrow", "d25"}
    d25_first = min(item.prediction_date for item in report.predictions if item.horizon == "d25")
    assert d25_first >= START + timedelta(days=60)
    assert all(len(fold.net_model_hash) == 64 and len(fold.severe_model_hash) == 64 for fold in report.folds)
    assert all(0.0 <= item.linear_severe_probability <= 1.0 for item in report.predictions)
    assert all(0.0 <= item.lightgbm_severe_probability <= 1.0 for item in report.predictions)
    assert len(linear.requests) == len(lightgbm.requests)
    assert linear.requests == lightgbm.requests


def test_shadow_report_is_order_independent_and_never_authorizes_production() -> None:
    days = tuple(day for index in range(66) for day in (_labeled_day(index, "tomorrow"), _labeled_day(index, "d25")))

    first = ScoreTomorrowShadowModels((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(days)
    second = ScoreTomorrowShadowModels((_RecordingTrainer("lightgbm"), _RecordingTrainer("linear"))).build(
        tuple(reversed(days))
    )

    assert first == second
    assert first.production_authority is False
    with pytest.raises(ValueError, match="training identity"):
        replace(first, spec_hash="f" * 64)
    with pytest.raises(ValueError, match="must be unique"):
        replace(first, predictions=(first.predictions[0], first.predictions[0]))
    with pytest.raises(ValueError, match="security identity"):
        replace(first.predictions[0], board="invalid")


def test_shadow_settlement_rejects_horizon_and_observation_lag_mismatch() -> None:
    basis = _labeled_day(0, "tomorrow").settlements[0].basis

    with pytest.raises(ValueError, match="horizon and observation lag"):
        ShadowSettlementLabel(horizon="d25", observation_lag=1, basis=basis)


def test_shadow_models_reject_cross_horizon_feature_drift() -> None:
    tomorrow = _labeled_day(0, "tomorrow")
    d25 = _labeled_day(0, "d25")
    drifted = replace(d25, features=replace(d25.features, context_hash="f" * 64))

    with pytest.raises(ValueError, match="same point-in-time feature batch"):
        ScoreTomorrowShadowModels((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(
            (tomorrow, drifted)
        )
    with pytest.raises(ValueError, match="same feature dates"):
        ScoreTomorrowShadowModels((_RecordingTrainer("linear"), _RecordingTrainer("lightgbm"))).build(
            (tomorrow, _labeled_day(1, "d25"))
        )


def _labeled_day(index: int, horizon: str) -> ShadowLabeledDay:
    trade_date = START + timedelta(days=index)
    observed_at = datetime.combine(trade_date, time(14, 50), tzinfo=SHANGHAI)
    rows = tuple(_feature_row(code, position, index, observed_at) for position, code in enumerate(CODES))
    batch = TomorrowPointInTimeFeatureBatch(
        trade_date=trade_date,
        observed_at=observed_at,
        input_hash=f"{(index % 15) + 1:x}" * 64,
        context_hash=f"{((index + 1) % 15) + 1:x}" * 64,
        rows=rows,
    )
    label_lag = 1 if horizon == "tomorrow" else 25
    settlements = tuple(
        ShadowSettlementLabel(
            horizon=horizon,
            observation_lag=label_lag,
            basis=CostSettlementBasis(
                code=code,
                board=rows[position].board,
                decision_date=trade_date,
                label_date=trade_date + timedelta(days=label_lag),
                gross_excess_return=(index % 9 - 4) * 0.003 + position * 0.001,
                mae_atr20=-1.8 if (index + position) % 11 == 0 else -0.4,
                turnover=0.4 + position * 0.1,
            ),
        )
        for position, code in enumerate(CODES)
    )
    return ShadowLabeledDay(horizon=horizon, features=batch, settlements=settlements)


def _feature_row(code: str, position: int, index: int, observed_at: datetime) -> TomorrowStockFeatures:
    board = "chinext" if code.startswith("300") else "star" if code.startswith("688") else "main"
    values = tuple(
        TomorrowFeatureValue(
            name=name,
            family=_family(name),
            value=(index + position + feature_index + 1) / 100.0,
        )
        for feature_index, name in enumerate(TOMORROW_FEATURE_NAMES)
    )
    return TomorrowStockFeatures(
        code=code,
        board=board,
        industry="equipment",
        industry_effective_at=observed_at - timedelta(days=300),
        industry_received_at=observed_at - timedelta(days=200),
        as_of=observed_at,
        market_cap=1_000_000_000.0 + position * 100_000_000.0,
        liquidity=20_000_000.0 + position * 1_000_000.0,
        values=values,
        missing_fields=(),
        published_facts=(),
    )


def _family(name: str) -> str:
    if name.startswith("residual_reversal"):
        return "residual_reversal"
    if name.startswith("residual_momentum"):
        return "residual_momentum"
    if name == "overnight_gap":
        return "overnight"
    if name in {"intraday_return", "morning_return", "afternoon_return"}:
        return "intraday"
    return "tail"
