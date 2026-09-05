from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from tests.component.market_data_test_support import (
    _SHANGHAI,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    TAIL_POLICY,
    BoundedExecutor,
    DataPlaneRepository,
    FeatureBuilder,
    MarketDataGateway,
    SourceLaneRegistry,
    SourceObservation,
    StaticHistoryClient,
    StaticMarketClient,
    StaticTencentClient,
    _quote,
    _service,
)
from trader.infra.market_data.providers.exchange_security_master import (
    ExchangeSecurityMasterClient,
    ExchangeSecurityMasterListing,
)


def _listing(code: str, board: str, exchange: str) -> ExchangeSecurityMasterListing:
    return ExchangeSecurityMasterListing(
        code=code,
        name=f"证券{code}",
        listing_date=date(2020, 1, 2),
        board=board,
        exchange=exchange,
    )


def test_official_exchange_security_master_closes_listing_coverage_and_persists_snapshot(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2026, 8, 28, 10, 0, tzinfo=_SHANGHAI)
    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=3,
        sse_fetcher=lambda _timeout: (
            _listing("600001", "main", "SSE"),
            _listing("688001", "star", "SSE"),
        ),
        szse_fetcher=lambda _timeout: (_listing("300001", "chinext", "SZSE"),),
        wall_clock=lambda: observed_at,
    )
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((_quote(),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        listing_open_dates=lambda: (date(2020, 1, 2), date(2026, 8, 28)),
        wall_clock=lambda: observed_at,
    )
    data_plane = DataPlaneRepository(tmp_path)
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        data_plane=data_plane,
        exchange_security_master_client=client,
        wall_clock=lambda: observed_at,
    )

    service.references.schedule_reference_data(
        ("600001",),
        observed_at,
        security_master_codes=("600001", "688001", "300001"),
    )

    references = gateway.reference_observations(("600001", "688001", "300001"))
    assert len(references) == 3
    assert all(item.fields.get("listing_age_sessions") == 2.0 for item in references)
    assert all(item.source == "exchange_security_master" for item in references)
    refreshed_quotes = tuple(gateway.fetch_market(observed_at=observed_at))
    assert len(refreshed_quotes) == 1
    refreshed_quote = refreshed_quotes[0]
    assert refreshed_quote.board_source == "exchange"
    assert refreshed_quote.board_reliability == "verified"
    assert "missing_listing_date" not in refreshed_quote.execution_restrictions
    assert "missing_listing_age_sessions" not in refreshed_quote.execution_restrictions
    source_health = service.health()["sources"]["exchange"]
    assert source_health["success_count"] == 1
    assert source_health["snapshot_rows"] == 3
    assert source_health["listing_date_rows"] == 3
    assert source_health["last_error"] is None
    assert tuple(record.code for record in data_plane.load_security_master_recent_records()) == (
        "300001",
        "600001",
        "688001",
    )


def test_failed_official_exchange_refresh_retains_previous_security_master() -> None:
    observed_at = datetime(2026, 8, 28, 10, 0, tzinfo=_SHANGHAI)
    existing = SourceObservation(
        source="eastmoney_security_master",
        subject_key="600001",
        observed_at=observed_at,
        source_time=observed_at,
        received_at=observed_at,
        effective_at=observed_at,
        data_version="existing-source",
        fields={"board": "main", "exchange": "SSE", "listing_date": "2020-01-02"},
        missing_reasons={},
        payload_hash="existing-source",
        status="success",
        error_code=None,
    )
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((_quote(),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        wall_clock=lambda: observed_at,
    )
    gateway.update_reference_observations((existing,))
    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=2,
        sse_fetcher=lambda _timeout: (),
        szse_fetcher=lambda _timeout: (),
        wall_clock=lambda: observed_at,
    )
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        exchange_security_master_client=client,
        wall_clock=lambda: observed_at,
    )

    with pytest.raises(ValueError, match="security master snapshot"):
        service.references.schedule_reference_data(
            ("600001",),
            observed_at,
            security_master_codes=("600001", "600002"),
        )

    retained = gateway.reference_observations(("600001",))
    assert len(retained) == 1
    assert retained[0].source == "eastmoney_security_master"
    assert retained[0].data_version == "existing-source"


def test_official_security_master_refresh_is_independent_from_quote_deadline() -> None:
    observed_at = datetime(2026, 8, 28, 10, 0, tzinfo=_SHANGHAI)
    started = threading.Event()
    release = threading.Event()
    pool = BoundedExecutor(worker_count=5, queue_capacity=5, thread_name_prefix="source-data")
    lanes = SourceLaneRegistry(pool)

    def fetch_sse(_timeout: float) -> tuple[ExchangeSecurityMasterListing, ...]:
        started.set()
        assert release.wait(1.0)
        return (_listing("600001", "main", "SSE"),)

    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=2,
        sse_fetcher=fetch_sse,
        szse_fetcher=lambda _timeout: (_listing("300001", "chinext", "SZSE"),),
        wall_clock=lambda: observed_at,
    )
    gateway = MarketDataGateway(
        StaticMarketClient((_quote(),)),
        StaticMarketClient((replace(_quote(), source="sina"),)),
        StaticTencentClient(()),
        minimum_market_rows=1,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=30,
        listing_open_dates=lambda: (date(2020, 1, 2), date(2026, 8, 28)),
        wall_clock=lambda: observed_at,
    )
    service = _service(
        gateway,
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY),
        exchange_security_master_client=client,
        worker_pool=pool,
        source_lanes=lanes,
        wall_clock=lambda: observed_at,
    )
    pool.start()

    try:
        service.references.schedule_reference_data(
            ("600001",),
            observed_at,
            security_master_codes=("600001", "300001"),
        )
        assert started.wait(1.0)

        quotes = gateway.fetch_market(observed_at=observed_at, deadline=observed_at + timedelta(seconds=1))

        assert len(quotes) == 1
        assert quotes[0].price is not None
        release.set()
        deadline = time.monotonic() + 1.0
        while client.health().success_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.health().success_count == 1
    finally:
        release.set()
        lanes.stop(wait=True, timeout_seconds=1.0)
        pool.stop(wait=True, cancel_futures=True)
