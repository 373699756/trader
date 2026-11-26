"""Official download identities must reject an incomplete market snapshot."""

from dataclasses import replace
from datetime import date

import pytest

from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.infra.exchange_security_universe import (
    LoadCurrentAShareUniverse,
    SelectStEligibleUniverse,
)
from trader.infra.market_data.observations import ExchangeSecurityMasterListing


def _listings(exchange: str) -> tuple[ExchangeSecurityMasterListing, ...]:
    prefix = 600000 if exchange == "SSE" else 1
    growth_board = "star" if exchange == "SSE" else "chinext"
    return tuple(
        ExchangeSecurityMasterListing(f"{prefix + index:06d}", "fixture", date(2000, 1, 1),
                                     growth_board if index == 0 else "main", exchange)
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

    messages = []
    universe = LoadCurrentAShareUniverse(fetch_sse, fetch_szse, 15.0, source_label="fixture-exchanges", report=messages.append)

    assert calls == [("SSE", 15.0), ("SZSE", 15.0)]
    assert tuple(item.code for item in universe) == tuple(sorted(item.code for item in (*sse, *szse)))
    assert all(item.listed_on == date(2000, 1, 1) and item.delisted_on is None for item in universe)
    assert all(item.source_version == "exchange_security_master" for item in universe)
    assert all(message.fields[-1].text == "来源 fixture-exchanges" for message in messages)


@pytest.mark.parametrize(("invalid", "reason"), (
    ("partial", "reference_universe_incomplete"),
    ("duplicate", "reference_universe_duplicate"),
    ("missing_exchange", "reference_universe_exchanges_invalid"),
    ("unsupported_board", "reference_universe_board_invalid"),
    ("missing_board", "reference_universe_board_coverage_invalid"),
))
def test_incomplete_official_universe_is_rejected(invalid: str, reason: str) -> None:
    sse, szse = _listings("SSE"), _listings("SZSE")
    if invalid == "partial":
        szse = szse[:-1]
    elif invalid == "duplicate":
        szse = (replace(szse[0], code=sse[0].code), *szse[1:])
    elif invalid == "missing_exchange":
        szse = tuple(replace(item, exchange="SSE") for item in szse)
    elif invalid == "missing_board":
        sse = tuple(replace(item, board="main") for item in sse)
    else:
        szse = (replace(szse[0], board="unsupported"), *szse[1:])

    with pytest.raises(ValueError, match=reason):
        LoadCurrentAShareUniverse(lambda _timeout: sse, lambda _timeout: szse, 15.0, source_label="fixture-exchanges")


@pytest.mark.parametrize("exchange", ("SSE", "SZSE"))
def test_source_parse_failure_reports_exchange_and_safe_cause_before_summary(exchange):
    from trader.download.application.sync_data import SyncData
    from trader.download.domain.download_failure import DownloadValidationFailure

    original = ValueError("private supplier body /secret/path?token=secret")
    calls = []
    messages = []

    def fetch(source):
        def request(_timeout):
            calls.append(source)
            if source == exchange:
                raise original
            return _listings(source)
        return request

    def prepare():
        return LoadCurrentAShareUniverse(fetch("SSE"), fetch("SZSE"), 15.0, source_label="fixture", report=messages.append)

    with pytest.raises(DownloadValidationFailure) as caught:
        prepare()
    assert caught.value.__cause__ is original
    calls.clear()
    messages.clear()

    def forbidden(_reference):
        pytest.fail("download must not start after official population preparation fails")

    result = SyncData(prepare, forbidden, forbidden, lambda: False).execute("history")

    assert result.failure == f"preparation_reference_{exchange.lower()}_listings_invalid"
    assert result.tasks == () and result.exit_code == 1
    assert calls == (["SSE"] if exchange == "SSE" else ["SSE", "SZSE"])
    diagnostic = " ".join(field.text for field in messages[-1].fields)
    assert messages[-1].state == "失败" and messages[-1].finished
    assert f"reference_{exchange.lower()}_listings_invalid" in diagnostic
    assert "ValueError" in diagnostic and "test_exchange_security_universe" in diagnostic
    assert all(secret not in diagnostic for secret in ("private", "/secret", "token=", "supplier body"))
    assert not any(message.state == "完成" for message in messages)


def test_partial_population_reports_counts_and_never_reports_completion():
    messages = []
    with pytest.raises(ValueError, match="reference_universe_incomplete"):
        LoadCurrentAShareUniverse(lambda _: _listings("SSE"), lambda _: (), 15.0, source_label="fixture", report=messages.append)
    text = " ".join(field.text for field in messages[-1].fields)
    assert "population" in text and ">=4000" in text and "2000" in text
    assert not any(message.state == "完成" for message in messages)


def test_invalid_security_identity_is_not_reported_as_completed_population():
    messages = []
    sse = _listings("SSE")
    sse = (replace(sse[0], name=""), *sse[1:])
    with pytest.raises(ValueError, match="reference_security_identity_invalid"):
        LoadCurrentAShareUniverse(lambda _: sse, lambda _: _listings("SZSE"), 15.0, source_label="fixture", report=messages.append)
    assert messages[-1].state == "失败" and not any(message.state == "完成" for message in messages)


def test_exchange_request_failure_does_not_return_partial_universe() -> None:
    messages = []

    def unavailable(_timeout: float):
        raise TimeoutError("fixture timeout")

    with pytest.raises(TimeoutError):
        LoadCurrentAShareUniverse(lambda _timeout: _listings("SSE"), unavailable, 15.0, source_label="fixture-exchanges", report=messages.append)
    assert messages[-1].state == "失败"
    assert messages[-1].fields[-1].text == "来源 fixture-exchanges/SZSE"


def test_shared_st_eligibility_selects_the_exact_download_population() -> None:
    universe = LoadCurrentAShareUniverse(lambda _timeout: _listings("SSE"), lambda _timeout: _listings("SZSE"), 15.0, source_label="fixture-exchanges")
    eligible_codes = tuple(item.code for item in universe if not item.code.endswith("9"))
    evidence = tuple(
        HistoryStEvidence(
            item.code,
            date(2026, 10, 10),
            "clear" if item.code in eligible_codes else "ever_st",
        )
        for item in universe
    )

    selected = SelectStEligibleUniverse(universe, evidence)

    assert tuple(item.code for item in selected) == eligible_codes


@pytest.mark.parametrize("evidence", ((), (HistoryStEvidence("999999", date(2026, 10, 10), "clear"),)))
def test_downloads_fail_closed_without_complete_shared_st_evidence(evidence) -> None:
    universe = LoadCurrentAShareUniverse(lambda _timeout: _listings("SSE"), lambda _timeout: _listings("SZSE"), 15.0, source_label="fixture-exchanges")
    with pytest.raises(RuntimeError, match="historical ST evidence"):
        SelectStEligibleUniverse(universe, evidence)
