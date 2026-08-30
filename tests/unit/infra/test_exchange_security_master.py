from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from trader.infra.market_data import exchange_security_master as exchange_module
from trader.infra.market_data.exchange_security_master import (
    ExchangeSecurityMasterClient,
    ExchangeSecurityMasterListing,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_OBSERVED_AT = datetime(2026, 8, 28, 10, 0, tzinfo=_SHANGHAI)


def _listing(code: str, board: str, exchange: str) -> ExchangeSecurityMasterListing:
    return ExchangeSecurityMasterListing(
        code=code,
        name=f"证券{code}",
        listing_date=date(2020, 1, 2),
        board=board,
        exchange=exchange,
    )


def test_exchange_security_master_builds_one_complete_atomic_snapshot() -> None:
    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=3,
        sse_fetcher=lambda _timeout: (
            _listing("600001", "main", "SSE"),
            _listing("688001", "star", "SSE"),
        ),
        szse_fetcher=lambda _timeout: (_listing("300001", "chinext", "SZSE"),),
        wall_clock=lambda: _OBSERVED_AT,
    )

    observations = client.fetch(_OBSERVED_AT)

    assert tuple(item.subject_key for item in observations) == ("300001", "600001", "688001")
    assert all(item.source == "exchange_security_master" for item in observations)
    assert all(item.fields["listing_date"] == "2020-01-02" for item in observations)
    assert {item.fields["board"] for item in observations} == {"main", "chinext", "star"}
    assert len({item.data_version for item in observations}) == 1
    health = client.health()
    assert health.planned_count == 1
    assert health.success_count == 1
    assert health.error_count == 0
    assert health.snapshot_rows == 3
    assert health.listing_date_rows == 3
    assert health.last_error is None


def test_exchange_security_master_rejects_partial_or_conflicting_snapshot_without_returning_rows() -> None:
    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=3,
        sse_fetcher=lambda _timeout: (_listing("600001", "main", "SSE"),),
        szse_fetcher=lambda _timeout: (_listing("600001", "chinext", "SZSE"),),
        wall_clock=lambda: _OBSERVED_AT,
    )

    with pytest.raises(ValueError, match="security master snapshot"):
        client.fetch(_OBSERVED_AT)

    health = client.health()
    assert health.success_count == 0
    assert health.error_count == 1
    assert health.snapshot_rows == 0
    assert health.last_error == "invalid_snapshot"


def test_exchange_security_master_rejects_duplicate_code_even_when_rows_match() -> None:
    duplicate = _listing("600001", "main", "SSE")
    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=2,
        sse_fetcher=lambda _timeout: (duplicate, duplicate),
        szse_fetcher=lambda _timeout: (_listing("300001", "chinext", "SZSE"),),
        wall_clock=lambda: _OBSERVED_AT,
    )

    with pytest.raises(ValueError, match="security master snapshot"):
        client.fetch(_OBSERVED_AT)


def test_exchange_security_master_counts_source_staged_timeout() -> None:
    def timed_out(_timeout: float) -> tuple[ExchangeSecurityMasterListing, ...]:
        raise requests.Timeout("timed out")

    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=2,
        sse_fetcher=timed_out,
        szse_fetcher=lambda _timeout: (_listing("300001", "chinext", "SZSE"),),
        wall_clock=lambda: _OBSERVED_AT,
    )

    with pytest.raises(RuntimeError, match="sse security master fetch failed"):
        client.fetch(_OBSERVED_AT)

    health = client.health()
    assert health.error_count == 1
    assert health.timeout_count == 1
    assert health.last_error == "sse_timeout"


def test_exchange_security_master_does_not_accept_future_listing_date() -> None:
    future = ExchangeSecurityMasterListing(
        code="600001",
        name="未来证券",
        listing_date=date(2026, 8, 29),
        board="main",
        exchange="SSE",
    )
    client = ExchangeSecurityMasterClient(
        timeout_seconds=3.0,
        minimum_rows=2,
        sse_fetcher=lambda _timeout: (future,),
        szse_fetcher=lambda _timeout: (_listing("300001", "chinext", "SZSE"),),
        wall_clock=lambda: _OBSERVED_AT,
    )

    with pytest.raises(ValueError, match="security master snapshot"):
        client.fetch(_OBSERVED_AT)


def test_official_exchange_request_retries_one_transient_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

    attempts = 0

    def get(*_args: object, **_kwargs: object) -> Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise requests.ConnectionError("transient disconnect")
        return Response()

    monkeypatch.setattr(exchange_module.requests, "get", get)

    response = exchange_module._official_get(
        "https://example.invalid",
        params={"kind": "security-master"},
        timeout=1.0,
    )

    assert response.status_code == 200
    assert attempts == 2
