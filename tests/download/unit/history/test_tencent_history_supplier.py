from __future__ import annotations

import json

from dataclasses import replace

from datetime import date, timedelta

import pytest

from tests.helpers.download.qfq import QFQ_TENCENT_OPTIONS


from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistorySyncProgress, HistoryTencentSummary
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.entrypoints.sync_progress import SyncProgress

from trader.download.infra import tencent_daily
from trader.download.infra.history_supplier_router import FetchTencentHistoryWindow
from trader.download.infra.tencent_daily import DecodeTencentDailyRows, TencentDailyQuery

from trader.download.infra.tencent_qfq_supplier import (
    TencentQfqDependencies,
    TencentQfqSupplier,
    TencentSessionPool,
)

from trader.infra.http_contracts import HttpTimeout
from trader.recommendation.domain.outcomes.models import OutcomeTradingStatus
from trader.recommendation.infra.market_data.published_history_bars import OutcomeBars

from tests.helpers.download.history.tencent_history_supplier import (
    DAY,
    ROW,
    SECURITY,
    _Http,
    _PagedHttp,
    _SuspensionHttp,
)


def test_pairs_preserve_units_and_leave_unavailable_facts_missing():
    window = _Http().supplier().fetch_window(SECURITY, (DAY,))
    cell = window.cells[0]
    assert cell.status == "complete"
    assert cell.unadjusted.volume == 10000
    assert cell.unadjusted.amount == 200000
    assert cell.unadjusted.turnover == 1.2
    assert cell.unadjusted.preclose is None and cell.unadjusted.pct_change is None
    assert cell.qfq.turnover is None


def test_daily_request_totals_distinguish_raw_retry_and_adjusted_http(monkeypatch):
    now = [0.0]
    attempted_modes = []
    messages = []

    class TimedHttp(_Http):
        def get(self, url, **kwargs):
            mode = kwargs["params"]["param"].rsplit(",", 1)[-1]
            attempted_modes.append(mode)
            now[0] += 2.0 if mode == "bfq" else 3.0
            if len(attempted_modes) == 1:
                raise HttpTimeout("fixture")
            return super().get(url, **kwargs)

    decode = tencent_daily.DecodeTencentDailyPayload

    def decode_with_cost(response):
        now[0] += 10.0
        return decode(response)

    monkeypatch.setattr(tencent_daily, "DecodeTencentDailyPayload", decode_with_cost)
    http = TimedHttp()
    progress = StderrHistorySyncProgress(report=SyncProgress(messages.append, lambda: now[0]), monotonic=lambda: now[0])
    supplier = TencentQfqSupplier(
        TencentQfqDependencies(
            lambda: http,
            lambda _as_of: (SECURITY,),
            lambda: False,
            monotonic=lambda: now[0],
            progress=progress,
        ),
        replace(QFQ_TENCENT_OPTIONS, retries=1),
    )
    window = supplier.fetch_window(SECURITY, (DAY,))
    assert window.cells[0].status == "complete"
    assert attempted_modes == ["bfq", "bfq", "qfq"]
    assert now[0] == 27.0
    assert not messages

    progress.publish(HistorySyncProgress(
        "tencent_history", "completed", 1, 1, tencent_summary=HistoryTencentSummary(1, 1, 0, 0),
    ))
    fields = " | ".join(field.text for field in messages[-1].fields)
    assert "未复权 HTTP累计 4.000秒 2次 失败1 同端重试1" in fields
    assert "前复权 HTTP累计 3.000秒 1次 失败0 同端重试0" in fields


def _one_price_http():
    http = _SuspensionHttp()
    http.raw = http.adjusted = [[DAY.isoformat(), "9", "9", "9", "9", "100", {}, "1.2", "20", "0", "0"]]
    http.quote[3], http.quote[4], http.quote[5] = "9", "10", "9"
    http.quote[6], http.quote[32] = "100", "-10.00"
    http.quote[33], http.quote[34], http.quote[40] = "9", "9", "F"
    return http


@pytest.mark.parametrize(
    "price,change,status",
    [("9", "-10.00", OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN),
     ("11", "10.00", OutcomeTradingStatus.TRADABLE),
     ("10", "0.00", OutcomeTradingStatus.TRADABLE)],
)
def test_one_price_outcome_uses_same_session_raw_quote_without_extra_requests(price, change, status):
    http = _one_price_http()
    http.raw[0][1:5] = [price] * 4
    for index in (3, 5, 33, 34):
        http.quote[index] = price
    http.quote[32] = change

    window = http.supplier().fetch_window(SECURITY, (DAY,))

    assert len(http.calls) == 2
    raw, qfq = window.cells[0].unadjusted, window.cells[0].qfq
    assert raw.preclose == 10.0 and raw.pct_change == float(change)
    assert qfq.preclose is None and qfq.pct_change is None
    assert OutcomeBars(window.cells)[0].trading_status is status
    payload = {"data": {"sh600001": {"day": http.raw, "qt": {"sh600001": http.quote}}}}
    decoded = DecodeTencentDailyRows(json.dumps(payload), TencentDailyQuery("sh600001", DAY, DAY, "bfq", 1))
    assert decoded[0].preclose == raw.preclose and decoded[0].pct_change == raw.pct_change


@pytest.mark.parametrize(
    "position,value",
    [(2, "600002"), (30, "20261008161500"), (30, "20261010161500"),
     (30, "20261009145959"), (30, "20261009151599"), (30, "invalid"),
     (3, "8.9"), (5, "8.9"), (33, "8.9"), (34, "8.9"), (6, "99"), (40, "S"),
     (4, "0"), (4, "-10"), (4, "NaN"), (4, "inf"), (4, None), (4, True),
     (32, "-9"), (32, "NaN"), (32, "inf"), (32, None), (32, True)],
)
def test_unmatched_or_invalid_one_price_quote_preserves_unknown_outcome(position, value):
    http = _one_price_http()
    http.quote[position] = value

    window = http.supplier().fetch_window(SECURITY, (DAY,))

    assert len(http.calls) == 2
    assert window.cells[0].unadjusted.preclose is None
    assert window.cells[0].unadjusted.pct_change is None
    assert OutcomeBars(window.cells)[0].trading_status is OutcomeTradingStatus.UNKNOWN


def test_same_session_quote_cannot_supply_earlier_one_price_day_or_leak_into_next_request():
    http = _one_price_http()
    previous = DAY - timedelta(days=1)
    http.raw = http.adjusted = [[previous.isoformat(), *http.raw[0][1:]], http.raw[0]]
    supplier = http.supplier()

    window = supplier.fetch_window(SECURITY, (previous, DAY))

    assert tuple(bar.trading_status for bar in OutcomeBars(window.cells)) == (
        OutcomeTradingStatus.UNKNOWN, OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN,
    )
    assert window.cells[0].unadjusted.preclose is None
    assert window.cells[0].unadjusted.pct_change is None
    http.quote = []
    subsequent = supplier.fetch_window(SECURITY, (previous, DAY))
    assert all(bar.trading_status is OutcomeTradingStatus.UNKNOWN for bar in OutcomeBars(subsequent.cells))
    assert len(http.calls) == 4


def test_tencent_explicit_suspension_reuses_raw_response_and_skips_qfq():
    http = _SuspensionHttp()
    window = http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 1 and http.calls[0].endswith(",bfq")
    assert window.cells[0].status == "supplier_marked_suspended"
    assert window.cells[0].unadjusted.close_price is None
    assert window.cells[0].qfq.volume is None


def test_history_still_downloads_trading_days_before_the_suspended_tail():
    http = _SuspensionHttp()
    previous = DAY - timedelta(days=1)
    http.raw = http.adjusted = [[previous.isoformat(), *ROW[1:]]]
    window = http.supplier().fetch_window(SECURITY, (previous, DAY))
    assert tuple(cell.status for cell in window.cells) == ("complete", "supplier_marked_suspended")
    assert len(http.calls) == 2
    assert DAY.isoformat() not in http.calls[1]


@pytest.mark.parametrize(
    "position,value",
    [
        (2, "600002"), (40, ""), (40, "D"), (40, "U"),
        (30, "20261008161500"), (30, "20261010161500"), (30, "20261009103000"), (30, "invalid"),
        (5, "10"), (6, "100"), (33, "10"), (34, "10"), (37, "100"), (6, "bad"),
    ],
)
def test_unproven_full_day_suspension_remains_inference(position, value):
    http = _SuspensionHttp()
    http.quote[position] = value
    window = http.supplier().fetch_window(SECURITY, (DAY,))
    assert window.cells[0].status == "tencent_inferred_suspended"
    assert len(http.calls) == 2


def test_partial_price_failure_keeps_confirmed_suspension_and_other_dates_pending():
    http = _SuspensionHttp()
    start = DAY - timedelta(days=1)
    http.raw = [[start.isoformat(), *ROW[1:]]]
    http.fail_qfq = True
    with pytest.raises(HistorySupplierPendingError) as raised:
        http.supplier().fetch_window(SECURITY, (start, DAY))
    assert tuple(cell.status for cell in raised.value.window.cells) == ("qfq_missing", "supplier_marked_suspended")


def test_suspension_quote_does_not_leak_between_requests_or_replace_actual_trading():
    http = _SuspensionHttp()
    supplier = http.supplier()
    assert supplier.fetch_window(SECURITY, (DAY,)).cells[0].status == "supplier_marked_suspended"
    http.quote = []  # A subsequent request has no usable quote status.
    http.raw = http.adjusted = [ROW.copy()]
    assert supplier.fetch_window(SECURITY, (DAY,)).cells[0].status == "complete"
    http.quote = [""] * 88
    http.quote[2], http.quote[30], http.quote[40] = SECURITY.code, DAY.strftime("%Y%m%d") + "161500", "S"
    for index in (5, 6, 33, 34, 37):
        http.quote[index] = "0"
    assert supplier.fetch_window(SECURITY, (DAY,)).cells[0].status == "complete"


def test_cancellation_after_raw_response_stops_before_qfq():
    class CancelledHttp(_SuspensionHttp):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            self.cancelled = True
            return response

    http = CancelledHttp()
    with pytest.raises(RuntimeError, match="tencent_qfq_cancelled"):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 1


def test_raw_and_qfq_requests_reuse_one_http_session():
    sessions = []

    def create_session():
        session = _Http()
        sessions.append(session)
        return session

    supplier = TencentQfqSupplier(
        TencentQfqDependencies(create_session, lambda _as_of: (SECURITY,), lambda: False),
        QFQ_TENCENT_OPTIONS,
    )
    supplier.fetch_window(SECURITY, (DAY,))

    assert len(sessions) == 1
    assert len(sessions[0].calls) == 2
    assert all(",1," in request for request in sessions[0].calls)


def test_history_increment_pairs_latest_day_when_qfq_uses_two_rows_for_older_padding():
    days = tuple(date(2026, 10, 19) + timedelta(days=offset) for offset in range(5))
    previous = (date(2026, 10, 15), date(2026, 10, 16))

    class CountLimitedHttp(_Http):
        def get(self, url, **kwargs):
            param = kwargs["params"]["param"]
            count = int(param.split(",")[4])
            self.raw = [[day.isoformat(), *ROW[1:]] for day in days][:count]
            self.adjusted = [[day.isoformat(), *ROW[1:]] for day in (*previous, *days)][:count]
            return super().get(url, **kwargs)

    http = CountLimitedHttp()
    window = FetchTencentHistoryWindow(http.supplier(), SECURITY, days, days)

    assert all(cell.status == "complete" for cell in window.cells)
    assert window.cells[-1].trade_date == days[-1]
    assert all(",7," in request for request in http.calls)


def test_invalid_old_page_keeps_real_partial_pairs_without_switching_hosts():
    http = _Http()
    days = tuple(DAY - timedelta(days=1999 - index) for index in range(2000))
    http.raw = [[day.isoformat(), *ROW[1:]] for day in days]
    http.adjusted = http.raw[-640:]
    with pytest.raises(HistorySupplierPendingError) as pending:
        http.supplier().fetch_window(SECURITY, days, request_count=2000, allow_interior_padding=True)
    assert pending.value.reason == "tencent_qfq_page_invalid"
    window = pending.value.window
    assert len(http.calls) == 3
    assert all(",2000," in param for param in http.calls)
    assert sum(cell.status == "complete" for cell in window.cells) == 640
    assert sum(cell.status == "qfq_missing" for cell in window.cells) == 1360
    assert tuple(cell.trade_date for cell in window.cells) == days


def test_qfq_640_row_pages_fill_full_window_with_checked_overlap():
    days = tuple(DAY - timedelta(days=1999 - index) for index in range(2000))
    http = _PagedHttp(days)
    window = http.supplier().fetch_window(SECURITY, days, request_count=2000, allow_interior_padding=True)
    assert len(http.calls) == 5  # one raw request and four bounded qfq pages
    assert all(cell.status == "complete" for cell in window.cells)
    assert tuple(cell.trade_date for cell in window.cells) == days


@pytest.mark.parametrize("bad_prices", (["-0.1", "-0.1", "0.1", "-0.2"], ["0", "0", "0", "0"]))
def test_qfq_invalid_prices_leave_only_their_dates_missing_and_continue_older_pages(bad_prices):
    days = tuple(DAY - timedelta(days=1999 - index) for index in range(2000))
    http = _PagedHttp(days)
    http.adjusted = [row.copy() for row in http.raw]
    invalid = frozenset(days[100:127])
    for row in http.adjusted:
        if date.fromisoformat(row[0]) in invalid:
            row[1:5] = bad_prices

    with pytest.raises(HistorySupplierPendingError) as pending:
        http.supplier().fetch_window(SECURITY, days, request_count=2000, allow_interior_padding=True)

    window = pending.value.window
    assert pending.value.reason == "tencent_qfq_invalid_price"
    assert pending.value.unusable_qfq_dates == tuple(sorted(invalid))
    assert len(http.calls) == 5
    assert sum(cell.status == "complete" for cell in window.cells) == 1973
    assert {cell.trade_date for cell in window.cells if cell.status == "qfq_missing"} == invalid


def test_full_last_page_with_rows_before_target_start_is_not_stalled():
    days = tuple(DAY - timedelta(days=1999 - index) for index in range(2000))
    http = _PagedHttp(days)
    padding = [[(days[0] - timedelta(days=offset)).isoformat(), *ROW[1:]] for offset in reversed(range(1, 701))]
    http.adjusted = [*padding, *http.adjusted]

    window = http.supplier().fetch_window(SECURITY, days, request_count=2000, allow_interior_padding=True)

    assert len(http.calls) == 5
    assert all(cell.status == "complete" for cell in window.cells)


def test_invalid_qfq_with_no_raw_price_is_not_inferred_as_suspension():
    days = tuple(DAY - timedelta(days=offset) for offset in (2, 1, 0))
    http = _Http()
    http.raw = [[day.isoformat(), *ROW[1:]] for day in (days[0], days[2])]
    http.adjusted = [[day.isoformat(), *ROW[1:]] for day in days]
    http.adjusted[1][1:5] = ["-0.1", "-0.1", "0.1", "-0.2"]
    with pytest.raises(HistorySupplierPendingError) as pending:
        http.supplier().fetch_window(SECURITY, days)
    assert pending.value.reason == "tencent_qfq_invalid_price"
    assert pending.value.unusable_qfq_dates == (days[1],)
    assert pending.value.window.cells[1].status == "unknown_missing"


def test_invalid_qfq_on_non_target_padding_date_does_not_create_a_gap():
    days = (DAY - timedelta(days=2), DAY)
    middle = DAY - timedelta(days=1)
    http = _Http()
    http.raw = [[day.isoformat(), *ROW[1:]] for day in days]
    http.adjusted = [
        [days[0].isoformat(), *ROW[1:]],
        [middle.isoformat(), "0", "0", "0", "0", *ROW[5:]],
        [days[1].isoformat(), *ROW[1:]],
    ]

    window = http.supplier().fetch_window(SECURITY, days, allow_interior_padding=True)

    assert all(cell.status == "complete" for cell in window.cells)


@pytest.mark.parametrize("bad_price", ("nan", "invalid"))
def test_malformed_qfq_price_still_fails_closed(bad_price):
    http = _Http()
    http.adjusted[0][1] = bad_price
    with pytest.raises(ValueError):
        http.supplier().fetch_window(SECURITY, (DAY,))


def test_qfq_page_with_different_overlap_is_pending_and_keeps_verified_recent_rows():
    days = tuple(DAY - timedelta(days=1999 - index) for index in range(2000))
    http = _PagedHttp(days, conflicting_anchor=True)
    with pytest.raises(HistorySupplierPendingError) as pending:
        http.supplier().fetch_window(SECURITY, days, request_count=2000, allow_interior_padding=True)
    assert pending.value.reason == "tencent_qfq_anchor_conflict"
    assert sum(cell.status == "complete" for cell in pending.value.window.cells) == 640
    assert sum(cell.status == "qfq_missing" for cell in pending.value.window.cells) == 1360


def test_session_pool_reuses_bootstrap_owned_connection_between_stocks():
    session = _Http()
    pool = TencentSessionPool((session,))
    supplier = TencentQfqSupplier(
        TencentQfqDependencies(pool.borrow, lambda _as_of: (SECURITY,), lambda: False),
        QFQ_TENCENT_OPTIONS,
    )

    supplier.fetch_window(SECURITY, (DAY,))
    supplier.fetch_window(SECURITY, (DAY,))

    assert len(session.calls) == 4


def test_calendar_advances_independently_of_old_history():
    http = _Http()
    context = http.supplier().load_qfq_context(DAY, 251)
    assert context.calendar.open_dates[-1] == DAY
    assert len(context.calendar.open_dates) == 251
    assert context.universe == (SECURITY,)
    assert ",251,bfq" in http.calls[0]


def test_successful_two_sided_omission_is_inferred_but_one_sided_gap_is_not():
    http = _Http()
    days = tuple(DAY - timedelta(days=offset) for offset in (2, 1, 0))
    http.raw = [[day.isoformat(), *ROW[1:]] for day in (days[0], days[2])]
    http.adjusted = [[days[2].isoformat(), *ROW[1:]]]
    window = http.supplier().fetch_window(SECURITY, days)
    assert tuple(cell.status for cell in window.cells) == ("qfq_missing", "tencent_inferred_suspended", "complete")
    assert all(",3," in request for request in http.calls)


def test_missing_raw_response_shape_cannot_be_inferred_as_suspension():
    http = _Http()
    http.raw = None
    http.adjusted = []
    with pytest.raises(ValueError, match="tencent_daily_response_shape_invalid"):
        http.supplier().fetch_window(SECURITY, (DAY,))


def test_full_history_calendar_accepts_one_extra_index_session():
    http = _Http()
    http.calendar = [[(DAY - timedelta(days=offset)).isoformat()] for offset in reversed(range(2001))]
    context = http.supplier().load_history_context(DAY, 2000, (SECURITY,))
    assert context.calendar.open_dates == tuple(date.fromisoformat(row[0]) for row in http.calendar[-2000:])
    assert len(http.calls) == 1


@pytest.mark.parametrize("count", (640, 2000))
def test_stock_prices_accept_one_extra_row_and_trim_only_older_padding(count):
    http = _Http()
    days = tuple(DAY - timedelta(days=offset) for offset in reversed(range(count)))
    older = days[0] - timedelta(days=1)
    http.raw = [[day.isoformat(), *ROW[1:]] for day in (older, *days)]
    http.adjusted = http.raw
    window = http.supplier().fetch_window(SECURITY, days, request_count=count)
    assert tuple(cell.trade_date for cell in window.cells) == days
    assert all(cell.status == "complete" for cell in window.cells)
    assert len(http.calls) == 2


@pytest.mark.parametrize("invalid", ("duplicate", "future", "unordered", "too_many", "incomplete"))
def test_full_history_calendar_padding_does_not_relax_date_validation(invalid):
    http = _Http()
    http.calendar = [[(DAY - timedelta(days=offset)).isoformat()] for offset in reversed(range(2001))]
    if invalid == "duplicate":
        http.calendar[0] = http.calendar[1]
    elif invalid == "future":
        http.calendar[-1] = [(DAY + timedelta(days=1)).isoformat()]
    elif invalid == "unordered":
        http.calendar[0], http.calendar[1] = http.calendar[1], http.calendar[0]
    elif invalid == "too_many":
        http.calendar.insert(0, [(DAY - timedelta(days=2001)).isoformat()])
    else:
        http.calendar = http.calendar[-1999:]
    with pytest.raises(ValueError):
        http.supplier().load_history_context(DAY, 2000, (SECURITY,))


@pytest.mark.parametrize("invalid", ("duplicate", "future", "short", "nan", "range"))
def test_rejects_invalid_rows_without_silently_dropping_them(invalid):
    http = _Http()
    if invalid == "duplicate":
        http.raw.append(ROW.copy())
    elif invalid == "future":
        http.raw[0][0] = (DAY + timedelta(days=1)).isoformat()
    elif invalid == "short":
        http.raw = [[DAY.isoformat()]]
    elif invalid == "nan":
        http.raw[0][5] = "nan"
    else:
        http.raw[0][3] = "9"
    with pytest.raises(ValueError):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 1


def test_missing_one_side_remains_pending_and_is_not_suspended():
    http = _Http()
    http.adjusted = []
    cell = http.supplier().fetch_window(SECURITY, (DAY,)).cells[0]
    assert cell.status == "qfq_missing" and cell.qfq is None


def test_supplier_older_padding_is_trimmed_without_accepting_future_rows():
    http = _Http()
    older = ROW.copy()
    older[0] = (DAY - timedelta(days=1)).isoformat()
    http.raw.insert(0, older)
    http.adjusted.insert(0, older)
    assert len(http.supplier().fetch_window(SECURITY, (DAY,)).cells) == 1


@pytest.mark.parametrize("zero", ("0", "0.0", "0.00", "-0.00", "0e-9", 0, 0.0))
def test_neutral_adjustment_accepts_numeric_zero_evidence(zero):
    http = _Http()
    http.adjusted = {"day": [ROW.copy()]}
    http.adjusted["day"][0][9:11] = [zero, zero]
    cell = http.supplier().fetch_window(SECURITY, (DAY,)).cells[0]
    assert cell.status == "complete"
    assert cell.unadjusted.close_price == cell.qfq.close_price == 10.5
    assert cell.qfq.adjustment == "qfq"


@pytest.mark.parametrize("index", (9, 10))
@pytest.mark.parametrize("invalid", ("1", "-0.01", "1e-9999", "nan", "inf", "-inf", "bad", "", None, False, {}, []))
def test_neutral_adjustment_rejects_invalid_or_nonzero_evidence(index, invalid):
    http = _Http()
    http.adjusted = {"day": [ROW.copy()]}
    http.adjusted["day"][0][index] = invalid
    window = http.supplier().fetch_window(SECURITY, (DAY,))
    assert window.cells[0].qfq is None
    assert len(http.calls) == 2


@pytest.mark.parametrize("invalid", ("short", "adjustment_record", "mixed_rows"))
def test_neutral_adjustment_requires_evidence_on_every_row(invalid):
    http = _Http()
    row = ROW.copy()
    row[9:11] = ["0.00", "0.00"]
    http.adjusted = {"day": [row]}
    if invalid == "short":
        del row[10:]
    elif invalid == "adjustment_record":
        row[6] = {"fixture": "adjusted"}
    else:
        changed = row.copy()
        changed[9] = "1"
        http.adjusted["day"].append(changed)
    window = http.supplier().fetch_window(SECURITY, (DAY,))
    assert window.cells[0].qfq is None
    assert len(http.calls) == 2


def test_retries_transport_only_and_cancellation_stops_attempts():
    http = _Http()
    http.error = HttpTimeout()
    with pytest.raises(RuntimeError, match="tencent_history_pending"):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 6
    http.cancelled = True
    with pytest.raises(RuntimeError, match="cancelled"):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 6


def test_rejects_unbounded_or_nonfinite_options():
    for kwargs in ({"retries": 3}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 0}):
        with pytest.raises(ValueError):
            replace(QFQ_TENCENT_OPTIONS, **kwargs)
