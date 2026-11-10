from datetime import timedelta
from threading import Event

import pytest

from trader.recommendation.application.history_quotes import HistoryQuoteMonth, HistoryQuotes
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.domain.market.quote_freshness import DEFAULT_QUOTE_SESSION_TIMES
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from tests.helpers.recommendation.publication.freeze_queries import NOW, _FreezeRecords, _freeze


def _quote(now=NOW):
    return MarketQuote("600000", "浦发银行", 11, 10, 10, 11, 10, 10, None, None, None,
                       1, 100000000, None, 1000000000, "银行", "fixture", now, now, "test")


def _owner(source, clock=None, monotonic=None):
    record = CommittedFreezeRecord(_freeze(), NOW.replace(hour=15), "scheduled")
    return HistoryQuotes((_FreezeRecords(record), _FreezeRecords(record)), source,
                         clock or (lambda: NOW), lambda: (NOW.date() - timedelta(days=1), NOW.date()),
                         DEFAULT_QUOTE_SESSION_TIMES, **({"monotonic": monotonic} if monotonic else {}))


def test_months_deduplicate_profiles_and_enforce_cadence_and_expiration():
    tick = [0.0]
    calls = []

    def source(codes, now, deadline, cancelled):
        calls.append(tuple(codes))
        return (_quote(),)

    owner = _owner(source, monotonic=lambda: tick[0])
    month = HistoryQuoteMonth(NOW.year, NOW.month)
    owner.watch("a", (month,))
    owner.watch("b", (month,))
    owner.refresh()
    assert calls == [("600000",)]
    owner.refresh()
    assert len(calls) == 1
    tick[0] = 60
    owner.refresh()
    assert len(calls) == 2
    tick[0] = 121
    owner.refresh()
    assert not owner.cached_quotes(("600000",))


def test_replaced_month_and_released_demand_cannot_publish_old_response():
    owner = None

    def source(codes, now, deadline, cancelled):
        assert owner is not None
        owner.release("a")
        return (_quote(),)

    owner = _owner(source)
    owner.watch("a", (HistoryQuoteMonth(NOW.year, NOW.month),))
    owner.refresh()
    assert owner.cached_quotes(("600000",)) == {}


def test_close_reuses_valid_quote_without_redundant_requests():
    now = [NOW.replace(hour=15, minute=1)]
    tick = [0.0]
    calls = []

    def source(codes, requested_at, deadline, cancelled):
        calls.append(tuple(codes))
        return (_quote(NOW.replace(hour=15, minute=0)),)

    owner = _owner(source, clock=lambda: now[0], monotonic=lambda: tick[0])
    owner.watch("a", (HistoryQuoteMonth(NOW.year, NOW.month),))
    owner.refresh()
    tick[0] = 61
    owner.refresh()
    assert len(calls) == 1
    assert owner.quote_status(owner.cached_quotes(("600000",))["600000"], now[0]) == "live"


def test_failed_refresh_retains_quote_and_older_response_does_not_replace_it():
    tick = [0.0]
    response = [_quote()]

    def source(codes, now, deadline, cancelled):
        if not response:
            raise RuntimeError("fixture supplier failure")
        return tuple(response)

    owner = _owner(source, monotonic=lambda: tick[0])
    owner.watch("a", (HistoryQuoteMonth(NOW.year, NOW.month),))
    owner.refresh()
    response[0] = _quote(NOW - timedelta(seconds=10))
    tick[0] = 60
    owner.refresh()
    assert owner.cached_quotes(("600000",))["600000"].source_time == NOW
    response.clear()
    tick[0] = 90
    owner.watch("a", (HistoryQuoteMonth(NOW.year, NOW.month),))
    tick[0] = 121
    with pytest.raises(RuntimeError, match="fixture supplier failure"):
        owner.refresh()
    assert owner.cached_quotes(("600000",))["600000"].price == 11


def test_nontrading_day_accepts_previous_session_close_and_worker_stops():
    closing = NOW.replace(hour=15, minute=0)
    owner = _owner(lambda codes, now, deadline, cancelled: (_quote(closing),),
                   clock=lambda: NOW + timedelta(days=1))
    owner.watch("a", (HistoryQuoteMonth(NOW.year, NOW.month),))
    owner.refresh()
    quote = owner.cached_quotes(("600000",))["600000"]
    assert quote.source_time == closing
    assert owner.quote_status(quote, NOW + timedelta(days=1)) == "live"
    owner.start()
    assert owner.stop().completed


def test_http_watch_returns_before_background_source_and_get_never_fetches():
    from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices
    from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
    from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
    from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
    from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
    from trader.web import CreateApp

    called = Event()

    def source(codes, now, deadline, cancelled):
        called.set()
        return (_quote(NOW + timedelta(seconds=30)),)

    owner = _owner(source, clock=lambda: NOW + timedelta(minutes=1))

    class Clock:
        def now(self):
            return NOW + timedelta(minutes=1)

    record = CommittedFreezeRecord(_freeze(), NOW.replace(hour=15), "scheduled")
    query = UnifiedFreezeQueries(UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), _FreezeRecords(record), Clock())
    query.bind_history_quotes(owner)
    lane = UnifiedWebServices(query, UnifiedFreezeEventStream(), lambda: {})
    client = CreateApp(services=ProfiledWebServices({"v2": lane}, "v2", history_quotes=owner)).test_client()
    response = client.put("/api/freezes/history/watches/tab1", json={"months": [{"year": NOW.year, "month": NOW.month}]})
    assert response.status_code == 202
    assert not called.is_set()
    url = f"/api/freezes/tomorrow/history?profile=v2&date={NOW.date()}"
    assert client.get(url).json["items"][0]["quote"]["status"] == "missing"
    assert not called.is_set()
    owner.refresh()
    payload = client.get(url).json
    assert payload["items"][0]["quote"]["price"] == 11
    assert payload["items"][0]["anchor_to_now_pct"] is not None
    assert client.delete("/api/freezes/history/watches/tab1").status_code == 200
    assert client.put("/api/freezes/history/watches/tab1", json={"months": [{"year": True, "month": 1}]}).status_code == 400
