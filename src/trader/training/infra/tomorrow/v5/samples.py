"""Past complete-session sequence windows for Tomorrow V5."""

from collections import deque
from datetime import date

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, ModelPriceVolumeSequence, PriceVolumePoint
from trader.training.infra.price_volume import ProjectSequencePoint


def CompletedTomorrowSequence(
    points: deque[PriceVolumePoint] | None, expected_dates: tuple[date, ...], code: str, source_identity: str,
) -> ModelPriceVolumeSequence | None:
    if points is None or len(points) != len(expected_dates) or tuple(point.trade_date for point in points) != expected_dates:
        return None
    return CalculatePriceVolumeSequence(code, tuple(points), expected_dates=expected_dates, source_identity=source_identity)


def AppendTomorrowSequencePoints(
    rolling: dict[str, deque[PriceVolumePoint]], rows: tuple[HistoryTrainingRow, ...], length: int,
) -> None:
    for row in rows:
        projected = (
            ProjectSequencePoint(row.point)
            if row.point is not None and row.cell_status in {"complete", "supplier_marked_suspended"}
            else None
        )
        if projected is None:
            rolling.pop(row.code, None)
        else:
            points = rolling.get(row.code)
            if points is None:
                points = rolling[row.code] = deque(maxlen=length)
            points.append(projected)
