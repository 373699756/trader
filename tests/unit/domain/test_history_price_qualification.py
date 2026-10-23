from dataclasses import replace
from datetime import date

import pytest

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_price_qualification import QualifyPriceOverlap


def _partial_and_complete(amount: float) -> tuple[BaoStockDailyCell, BaoStockDailyCell]:
    day = date(2026, 10, 23)
    previous_raw = BaoStockDailySide(
        "000001",
        day,
        "unadjusted",
        11.27,
        11.35,
        11.17,
        11.2,
        144880600.0,
        1628280000.0,
        None,
        None,
        None,
        "trading",
    )
    incoming_raw = replace(previous_raw, amount=amount)
    incoming_qfq = replace(incoming_raw, adjustment="qfq")
    return (
        BaoStockDailyCell("000001", day, "qfq_missing", previous_raw, None),
        BaoStockDailyCell("000001", day, "complete", incoming_raw, incoming_qfq),
    )


def test_rounded_raw_amount_can_be_refined_when_missing_qfq_is_filled():
    previous, complete = _partial_and_complete(1628277200.0)

    assert QualifyPriceOverlap((complete,), (previous,), full_window=False) == (complete,)


@pytest.mark.parametrize("amount", (1628200000.0, 1628277200.0))
def test_material_raw_overlap_conflict_still_fails(amount):
    previous, complete = _partial_and_complete(amount)
    if amount == 1628277200.0:
        complete = replace(complete, unadjusted=replace(complete.unadjusted, close_price=11.19))

    with pytest.raises(RuntimeError, match="history_tail_raw_price_conflict"):
        QualifyPriceOverlap((complete,), (previous,), full_window=False)
