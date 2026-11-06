from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from trader.training.application.shadow_model_ports import ShadowFitRequest, ShadowFitResult
from trader.training.application.shadow_model_report import ShadowLabeledDay, ShadowSettlementLabel
from trader.training.application.tomorrow_feature_contracts import TomorrowPointInTimeFeatureBatch
from trader.training.domain.evaluation.historical import CostSettlementBasis
from trader.training.domain.evaluation.tomorrow_features import (
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


def _labeled_day(index: int, horizon: str) -> ShadowLabeledDay:
    trade_date = START + timedelta(days=index)
    observed_at = datetime.combine(trade_date, time(15, 0), tzinfo=SHANGHAI)
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
                freeze_date=trade_date,
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
