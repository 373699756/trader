from dataclasses import replace
from datetime import date

import pytest

from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_price_qualification import (
    HistoryQfqBasisError, NeedsPriceSupplement, QualifyPriceOverlap, QualifyReplacementWindow,
)
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow


def test_replacement_uses_whole_new_adjustment_basis():
    partial, complete = _partial_and_complete(1628280000.0)
    assert complete.qfq is not None
    previous = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(complete),))
    complete = replace(
        complete,
        qfq=replace(complete.qfq, open_price=3.0, high_price=3.1, low_price=2.9, close_price=3.0),
    )
    incoming = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(complete),))
    assert QualifyReplacementWindow((partial.trade_date,), previous, incoming) is incoming


@pytest.mark.parametrize("field,value", (("preclose", 11.0), ("pct_change", 1.8), ("turnover", 0.5)))
def test_replacement_does_not_erase_known_raw_auxiliary_fields(field, value):
    _, complete = _partial_and_complete(1628280000.0)
    known = replace(complete, unadjusted=replace(complete.unadjusted, **{field: value}))
    previous = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(known),))
    incoming = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(complete),))

    with pytest.raises(RuntimeError, match="price_replacement_known_raw_field_missing"):
        QualifyReplacementWindow((complete.trade_date,), previous, incoming)


def test_replacement_can_fill_auxiliary_fields_without_changing_parent_pair():
    _, complete = _partial_and_complete(1628280000.0)
    supplied = replace(complete, unadjusted=replace(complete.unadjusted, preclose=11.0, pct_change=1.8, turnover=0.5))
    previous = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(complete),))
    incoming = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(supplied),))

    assert QualifyReplacementWindow((complete.trade_date,), previous, incoming) is incoming
    assert previous.cells[0].unadjusted.pct_change is None
    assert previous.cells[0].qfq == incoming.cells[0].qfq


def test_supplement_selection_is_for_unconfirmed_sessions_and_one_price_unknown_change():
    _, complete = _partial_and_complete(1628280000.0)
    ordinary = ProjectHistoryCell(complete)
    one_price = replace(
        ordinary,
        unadjusted=replace(ordinary.unadjusted, open_price=11.2, high_price=11.2, low_price=11.2),
    )
    inferred = replace(ordinary, status="tencent_inferred_suspended", unadjusted=None, qfq=None)

    assert not NeedsPriceSupplement(ordinary)
    assert NeedsPriceSupplement(one_price)
    assert NeedsPriceSupplement(inferred)
    assert not NeedsPriceSupplement(replace(one_price, unadjusted=replace(one_price.unadjusted, pct_change=0.0)))
    assert not NeedsPriceSupplement(replace(one_price, unadjusted=replace(one_price.unadjusted, volume=0.0)))


@pytest.mark.parametrize("field", ("open_price", "high_price", "low_price", "close_price"))
def test_incremental_same_source_requires_unchanged_qfq_price_anchors(field):
    _partial, previous = _partial_and_complete(1628280000.0)
    incoming = replace(previous, qfq=replace(previous.qfq, **{field: getattr(previous.qfq, field) + 0.01}))
    with pytest.raises(HistoryQfqBasisError, match="history_qfq_basis_changed"):
        QualifyPriceOverlap((incoming,), (previous,), full_window=False)


def test_incremental_qfq_activity_refinement_does_not_change_price_basis():
    _partial, previous = _partial_and_complete(1628280000.0)
    incoming = replace(previous, qfq=replace(previous.qfq, volume=previous.qfq.volume + 100.0, amount=previous.qfq.amount + 100.0))
    assert QualifyPriceOverlap((incoming,), (previous,), full_window=False) == (previous,)


def test_new_trading_price_without_observed_qfq_overlap_requires_whole_window():
    _partial, previous = _partial_and_complete(1628280000.0)
    day = date(2026, 10, 26)
    incoming = replace(previous, trade_date=day, unadjusted=replace(previous.unadjusted, trade_date=day), qfq=replace(previous.qfq, trade_date=day))
    with pytest.raises(HistoryQfqBasisError, match="history_qfq_basis_unverified"):
        QualifyPriceOverlap((incoming,), (previous,), full_window=False)


def test_explicit_suspension_replaces_inference_without_fabricating_a_trading_price():
    _partial, complete = _partial_and_complete(1628280000.0)
    inferred = BaoStockDailyCell(complete.code, complete.trade_date, "tencent_inferred_suspended", None, None)
    confirmed = replace(
        complete,
        status="supplier_marked_suspended",
        unadjusted=replace(complete.unadjusted, trading_status="suspended"),
        qfq=replace(complete.qfq, trading_status="suspended"),
    )
    assert QualifyPriceOverlap((confirmed,), (inferred,), full_window=False) == (confirmed,)


def test_replacement_rejects_missing_dates_and_unresolved_cells():
    partial, complete = _partial_and_complete(1628280000.0)
    previous = PublishedHistoryWindow(partial.code, (ProjectHistoryCell(partial),))
    with pytest.raises(RuntimeError, match="incomplete"):
        QualifyReplacementWindow((partial.trade_date,), previous, previous)
    with pytest.raises(RuntimeError, match="identity_conflict"):
        QualifyReplacementWindow((partial.trade_date,), previous, PublishedHistoryWindow(partial.code, ()))
    incoming = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(complete),))
    with pytest.raises(RuntimeError, match="identity_conflict"):
        QualifyReplacementWindow((date(2026, 10, 22), partial.trade_date), previous, incoming)


def test_replacement_checks_raw_even_when_previous_qfq_is_missing():
    partial, complete = _partial_and_complete(1628280000.0)
    previous = PublishedHistoryWindow(partial.code, (ProjectHistoryCell(partial),))
    assert complete.unadjusted is not None
    altered = replace(complete, unadjusted=replace(complete.unadjusted, volume=1.0))
    incoming = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(altered),))
    with pytest.raises(RuntimeError, match="raw_price_conflict"):
        QualifyReplacementWindow((partial.trade_date,), previous, incoming)


def test_replacement_does_not_turn_observed_trading_into_suspension():
    partial, complete = _partial_and_complete(1628280000.0)
    previous = PublishedHistoryWindow(partial.code, (ProjectHistoryCell(partial),))
    assert complete.unadjusted is not None and complete.qfq is not None
    suspended = replace(
        complete,
        status="supplier_marked_suspended",
        unadjusted=replace(complete.unadjusted, trading_status="suspended", volume=0.0, amount=0.0),
        qfq=replace(complete.qfq, trading_status="suspended", volume=0.0, amount=0.0),
    )
    incoming = PublishedHistoryWindow(complete.code, (ProjectHistoryCell(suspended),))
    with pytest.raises(RuntimeError, match="trading_status_conflict"):
        QualifyReplacementWindow((partial.trade_date,), previous, incoming)


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
