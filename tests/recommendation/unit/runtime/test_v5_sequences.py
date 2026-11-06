from datetime import date, timedelta

import pytest

from trader.recommendation.domain.market.v5.features import CalculatePriceVolumeSequence, PriceVolumePoint


def test_complete_sequence_is_dated_and_does_not_bridge_missing_market_day():
    dates = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(61))
    points = tuple(PriceVolumePoint(day, 10 + i, 12 + i, 9 + i, 11 + i, 1000, 20000) for i, day in enumerate(dates))
    sequence = CalculatePriceVolumeSequence("600000", points, expected_dates=dates, source_identity="published")
    assert len(sequence.values) == 360
    assert sequence.values[0] == pytest.approx(11 / 11 - 1)
    assert sequence.values[3] == pytest.approx(12 / 11 - 1)
    with pytest.raises(ValueError, match="contiguous"):
        CalculatePriceVolumeSequence("600000", points[:-1], expected_dates=dates, source_identity="published")
    with pytest.raises(ValueError, match="contiguous"):
        CalculatePriceVolumeSequence(
            "600000", points, expected_dates=(*dates[:-1], dates[-1] + timedelta(days=1)), source_identity="published"
        )
