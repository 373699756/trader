"""Official download identities must reject an incomplete market snapshot."""

from dataclasses import replace
from datetime import date

import pytest

from trader.download.infra.exchange_security_universe import (
    load_current_a_share_universe,
    select_history_eligible_universe,
)
from trader.infra.market_data.providers.exchange_security_master import ExchangeSecurityMasterListing


def _listings(exchange: str) -> tuple[ExchangeSecurityMasterListing, ...]:
    prefix = 600000 if exchange == "SSE" else 1
    return tuple(
        ExchangeSecurityMasterListing(f"{prefix + index:06d}", "fixture", date(2000, 1, 1), "main", exchange)
        for index in range(2000)
    )


def test_current_universe_preserves_listing_identity_and_bounds_requests() -> None:
    sse, szse = _listings("SSE"), _listings("SZSE")
    calls: list[tuple[str, float]] = []

    def fetch_sse(timeout: float):
        calls.append(("SSE", timeout))
        return tuple(reversed(sse))

    def fetch_szse(timeout: float):
        calls.append(("SZSE", timeout))
        return szse

    universe = load_current_a_share_universe(fetch_sse, fetch_szse, 15.0)

    assert calls == [("SSE", 15.0), ("SZSE", 15.0)]
    assert tuple(item.code for item in universe) == tuple(sorted(item.code for item in (*sse, *szse)))
    assert all(item.listed_on == date(2000, 1, 1) and item.delisted_on is None for item in universe)
    assert all(item.source_version == "exchange_security_master" for item in universe)


@pytest.mark.parametrize("invalid", ("partial", "duplicate", "missing_exchange", "unsupported_board"))
def test_incomplete_official_universe_is_rejected(invalid: str) -> None:
    sse, szse = _listings("SSE"), _listings("SZSE")
    if invalid == "partial":
        szse = szse[:-1]
    elif invalid == "duplicate":
        szse = (replace(szse[0], code=sse[0].code), *szse[1:])
    elif invalid == "missing_exchange":
        szse = tuple(replace(item, exchange="SSE") for item in szse)
    else:
        szse = (replace(szse[0], board="unsupported"), *szse[1:])

    with pytest.raises(ValueError, match="universe is incomplete"):
        load_current_a_share_universe(lambda _timeout: sse, lambda _timeout: szse, 15.0)


def test_exchange_request_failure_does_not_return_partial_universe() -> None:
    def unavailable(_timeout: float):
        raise TimeoutError("fixture timeout")

    with pytest.raises(TimeoutError):
        load_current_a_share_universe(lambda _timeout: _listings("SSE"), unavailable, 15.0)


def test_history_eligibility_selects_the_exact_qfq_population() -> None:
    universe = load_current_a_share_universe(
        lambda _timeout: _listings("SSE"), lambda _timeout: _listings("SZSE"), 15.0
    )
    eligible_codes = tuple(item.code for item in universe if not item.code.endswith("9"))

    selected = select_history_eligible_universe(universe, eligible_codes)

    assert tuple(item.code for item in selected) == eligible_codes


@pytest.mark.parametrize("eligible_codes", ((), ("999999",)))
def test_qfq_fails_closed_without_a_matching_history_eligibility(eligible_codes) -> None:
    universe = load_current_a_share_universe(
        lambda _timeout: _listings("SSE"), lambda _timeout: _listings("SZSE"), 15.0
    )
    with pytest.raises(RuntimeError, match="history eligible universe"):
        select_history_eligible_universe(universe, eligible_codes)
