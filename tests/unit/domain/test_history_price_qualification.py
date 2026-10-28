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


@pytest.mark.parametrize("amount", (1626000000.0, 1631000000.0))
def test_material_raw_amount_conflict_still_fails(amount):
    previous, complete = _partial_and_complete(amount)

    with pytest.raises(RuntimeError, match="history_tail_raw_price_conflict"):
        QualifyPriceOverlap((complete,), (previous,), full_window=False)


@pytest.mark.parametrize("volume", (144800600.0, 144960600.0))
def test_small_activity_revisions_fill_gap_and_preserve_existing_complete_pair(volume):
    previous, complete = _partial_and_complete(1628200000.0)
    incoming = replace(
        complete,
        unadjusted=replace(complete.unadjusted, volume=volume),
        qfq=replace(complete.qfq, volume=volume),
    )

    assert QualifyPriceOverlap((incoming,), (previous,), full_window=False) == (incoming,)
    existing = replace(previous, status="complete", qfq=replace(previous.unadjusted, adjustment="qfq"))
    assert QualifyPriceOverlap((incoming,), (existing,), full_window=False) == (existing,)


@pytest.mark.parametrize("field", ("volume", "amount"))
@pytest.mark.parametrize("smaller, accepted", ((999001.0, True), (999000.0, False)))
@pytest.mark.parametrize("reverse", (False, True))
def test_activity_relative_tolerance_boundary_is_symmetric(field, smaller, accepted, reverse):
    previous, complete = _partial_and_complete(1628280000.0)
    before, after = (smaller, 1000001.0) if reverse else (1000001.0, smaller)
    previous = replace(previous, unadjusted=replace(previous.unadjusted, **{field: before}))
    complete = replace(
        complete,
        unadjusted=replace(complete.unadjusted, **{field: after}),
        qfq=replace(complete.qfq, **{field: after}),
    )

    if accepted:
        assert QualifyPriceOverlap((complete,), (previous,), full_window=False) == (complete,)
    else:
        with pytest.raises(RuntimeError, match="history_tail_raw_price_conflict"):
            QualifyPriceOverlap((complete,), (previous,), full_window=False)


def test_whole_ten_thousand_amount_rounding_remains_valid_above_relative_tolerance():
    previous, complete = _partial_and_complete(197200.0)
    previous = replace(previous, unadjusted=replace(previous.unadjusted, amount=200000.0))

    assert QualifyPriceOverlap((complete,), (previous,), full_window=False) == (complete,)


@pytest.mark.parametrize("field", ("volume", "amount"))
def test_small_activity_totals_keep_absolute_tolerance(field):
    previous, complete = _partial_and_complete(1628280000.0)
    previous = replace(previous, unadjusted=replace(previous.unadjusted, **{field: 1001.0}))
    complete = replace(
        complete,
        unadjusted=replace(complete.unadjusted, **{field: 1101.0}),
        qfq=replace(complete.qfq, **{field: 1101.0}),
    )

    assert QualifyPriceOverlap((complete,), (previous,), full_window=False) == (complete,)


@pytest.mark.parametrize(
    "field, value", (("open_price", 11.28), ("high_price", 11.36), ("low_price", 11.16), ("close_price", 11.19))
)
def test_one_cent_raw_price_conflict_still_fails(field, value):
    previous, complete = _partial_and_complete(1628200000.0)
    complete = replace(complete, unadjusted=replace(complete.unadjusted, **{field: value}))

    with pytest.raises(RuntimeError, match="history_tail_raw_price_conflict"):
        QualifyPriceOverlap((complete,), (previous,), full_window=False)


def test_trading_status_conflict_still_fails():
    previous, complete = _partial_and_complete(1628280000.0)
    suspended = BaoStockDailySide(
        previous.code,
        previous.trade_date,
        "unadjusted",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "suspended",
    )
    complete = replace(
        complete, status="supplier_marked_suspended", unadjusted=suspended, qfq=replace(suspended, adjustment="qfq")
    )

    with pytest.raises(RuntimeError, match="history_tail_raw_price_conflict"):
        QualifyPriceOverlap((complete,), (previous,), full_window=False)
