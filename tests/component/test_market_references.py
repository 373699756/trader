from __future__ import annotations

from tests.component.market_data_test_support import (
    NOW,
    Board,
    MarketDataGateway,
    SequenceMarketClient,
    SourceObservation,
    StaticMarketClient,
    StaticTencentClient,
    _quote,
    date,
    datetime,
    replace,
    timedelta,
)


def test_newer_reference_refresh_can_correct_an_older_effective_listing_date() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def master(listing_date: str, source_time: datetime, version: str) -> SourceObservation:
        return SourceObservation(
            source="tushare",
            subject_key="600001",
            observed_at=source_time,
            source_time=source_time,
            received_at=source_time,
            effective_at=datetime.fromisoformat(f"{listing_date}T00:00:00+08:00"),
            data_version=version,
            fields={"board": "main", "listing_date": listing_date},
            missing_reasons={},
            payload_hash=version,
            status="success",
            error_code=None,
        )

    gateway.update_reference_observations((master("2020-01-02", NOW - timedelta(seconds=1), "master-initial"),))
    gateway.update_reference_observations((master("2019-01-02", NOW, "master-latest"),))
    gateway.fetch_market(observed_at=NOW)

    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None
    assert snapshot.quotes[0].listing_date == date(2019, 1, 2)


def test_listing_session_projection_reuses_a_sorted_calendar_index() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        wall_clock=lambda: NOW,
    )

    def calendar(day: date) -> SourceObservation:
        effective_at = datetime.combine(day, datetime.min.time(), tzinfo=NOW.tzinfo)
        return SourceObservation(
            source="tushare",
            subject_key=day.isoformat(),
            observed_at=NOW,
            source_time=NOW,
            received_at=NOW,
            effective_at=effective_at,
            data_version="calendar-current",
            fields={"calendar_date": day.isoformat(), "is_open": True},
            missing_reasons={},
            payload_hash=day.isoformat(),
            status="success",
            error_code=None,
        )

    gateway.update_reference_observations((calendar(date(2020, 1, 2)), calendar(date(2026, 7, 16))))

    class NoIterationOpenDates(set[date]):
        def __iter__(self):
            raise AssertionError("listing-age projection must not rescan every calendar date per security")

    gateway._calendar_open_dates = NoIterationOpenDates((date(2020, 1, 2), date(2026, 7, 16)))
    master = SourceObservation(
        source="tushare",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=datetime.fromisoformat("2020-01-02T00:00:00+08:00"),
        data_version="master-initial",
        fields={"board": "main", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="master-initial",
        status="success",
        error_code=None,
    )

    gateway.update_reference_observations((master,))
    quote = gateway.fetch_market(observed_at=NOW)[0]

    assert quote.listing_age_sessions == 2


def test_listing_session_projection_uses_injected_production_calendar() -> None:
    calendar_requests: list[bool] = []

    def listing_open_dates() -> tuple[date, ...]:
        calendar_requests.append(True)
        return (date(2020, 1, 2), date(2020, 1, 3), date(2026, 7, 16))

    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="eastmoney"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        listing_open_dates=listing_open_dates,
        wall_clock=lambda: NOW,
    )
    master = SourceObservation(
        source="eastmoney_security_master",
        subject_key="600001",
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=datetime.fromisoformat("2020-01-02T00:00:00+08:00"),
        data_version="master-initial",
        fields={"board": "main", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="master-initial",
        status="success",
        error_code=None,
    )

    gateway.update_reference_observations((master,))
    quote = gateway.fetch_market(observed_at=NOW)[0]

    assert quote.listing_age_sessions == 3
    assert calendar_requests == [True]


def test_gateway_retains_official_security_master_when_realtime_source_falls_back() -> None:
    market_quote = replace(_quote(), source="tencent")
    second_observed_at = NOW + timedelta(seconds=1)
    sina_quote = replace(
        _quote(),
        source="sina",
        source_time=second_observed_at,
        received_time=second_observed_at,
        data_version="sina-history",
    )
    gateway = MarketDataGateway(
        SequenceMarketClient(((market_quote,), RuntimeError("tencent offline"))),
        SequenceMarketClient(((sina_quote,),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        wall_clock=lambda: second_observed_at,
    )
    gateway.update_reference_observations(
        (
            SourceObservation(
                source="exchange",
                subject_key="600001",
                observed_at=NOW,
                source_time=NOW,
                received_at=NOW,
                effective_at=NOW,
                data_version="official-master",
                fields={"board": "main", "exchange": "SSE", "listing_date": "1999-11-10"},
                missing_reasons={},
                payload_hash="official-master",
                status="success",
                error_code=None,
            ),
        )
    )

    first = tuple(gateway.fetch_market(observed_at=NOW))
    second = tuple(gateway.fetch_market(observed_at=second_observed_at, force=True))

    assert first[0].listing_date == date(1999, 11, 10)
    assert second[0].source == "sina"
    assert second[0].board is Board.MAIN
    assert second[0].board_source == "exchange"
    assert second[0].board_reliability == "verified"
    assert second[0].exchange == "SSE"
    assert second[0].listing_date == date(1999, 11, 10)


def test_newer_sparse_official_identity_cannot_delete_existing_listing_fields() -> None:
    gateway = MarketDataGateway(
        StaticMarketClient((replace(_quote(), source="tencent"),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        listing_open_dates=lambda: (date(2020, 1, 2), date(2020, 1, 3), date(2026, 7, 16)),
        wall_clock=lambda: NOW,
    )
    rich = SourceObservation(
        source="exchange",
        subject_key="600001",
        observed_at=NOW - timedelta(seconds=1),
        source_time=NOW - timedelta(seconds=1),
        received_at=NOW - timedelta(seconds=1),
        effective_at=NOW - timedelta(seconds=1),
        data_version="exchange-master-rich",
        fields={"board": "main", "exchange": "SSE", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="rich-master",
        status="success",
        error_code=None,
    )
    sparse = replace(
        rich,
        observed_at=NOW,
        source_time=NOW,
        received_at=NOW,
        effective_at=NOW,
        data_version="exchange-master-sparse",
        fields={"board": "main", "exchange": "SSE"},
        payload_hash="sparse-master",
    )

    gateway.update_reference_observations((rich,))
    gateway.update_reference_observations((sparse,))
    reference = gateway.reference_observations(("600001",))[0]
    quotes = gateway.fetch_market(observed_at=NOW)
    assert quotes
    quote = quotes[0]
    security_master = gateway.health().security_master

    assert reference.data_version == "exchange-master-sparse"
    assert reference.fields["listing_date"] == "2020-01-02"
    assert reference.fields["listing_age_sessions"] == 3.0
    assert quote.listing_date == date(2020, 1, 2)
    assert quote.listing_age_sessions == 3
    assert security_master.total_rows == 1
    assert security_master.listing_date_rows == 1
    assert security_master.listing_age_rows == 1
    assert security_master.complete_rows == 1
    assert security_master.provider == "free_market+production_calendar"
    assert security_master.tushare_required is False
    assert security_master.persistence_schedule_error_count == 0
