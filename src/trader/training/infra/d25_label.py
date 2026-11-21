"""Price evidence shared by the D25 sample builders."""

from collections.abc import Sequence
from decimal import Decimal

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.outcomes.prediction_target import EvaluatePredictionTarget, PredictionTarget
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain


def VerifiedD25Close(row: HistoryTrainingRow) -> float | None:
    point = row.point
    if (
        point is None or point.raw_close_price is None or point.raw_close_price <= 0
        or point.qfq_reconstruction_anchor is not None
        or row.cell_status not in {"complete", "supplier_marked_suspended"}
    ):
        return None
    return point.qfq_close_price


def D25HitLabel(
    target: PredictionTarget, signal_close: float | None, future_closes: Sequence[float | None],
) -> int | None:
    result = EvaluatePredictionTarget(
        target,
        Decimal(str(signal_close)) if signal_close is not None else None,
        tuple(Decimal(str(close)) if close is not None else None for close in future_closes),
    )
    if result is TomorrowCloseGain.PENDING:
        return None
    return int(result is TomorrowCloseGain.HIT)
