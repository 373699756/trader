from __future__ import annotations

import pytest

from trader.application.ports.market import MarketDataFailedError, MarketDataNoDataError
from trader.infra.market_data.router import RouteOutcome, VendorRoute, VendorSeverity, route


def test_route_prefers_required_success_after_optional_failure() -> None:
    calls: list[str] = []

    def optional_fail() -> tuple[tuple[str], ...]:
        calls.append("optional")
        raise RuntimeError("service unavailable")

    def primary_success() -> tuple[tuple[str], ...]:
        calls.append("eastmoney")
        return (("000001",),)

    outcome: RouteOutcome = route(
        (
            VendorRoute("research", optional_fail, VendorSeverity.OPTIONAL),
            VendorRoute("eastmoney", primary_success, VendorSeverity.REQUIRED),
        )
    )

    assert outcome.vendor == "eastmoney"
    assert outcome.degraded is True
    assert calls == ["optional", "eastmoney"]
    assert outcome.results[0].name == "research"
    assert outcome.results[0].status == "failed"
    assert outcome.results[0].severity is VendorSeverity.OPTIONAL
    assert outcome.results[0].result is None
    assert outcome.results[0].error == "service unavailable"
    assert outcome.results[1].name == "eastmoney"
    assert outcome.results[1].status == "success"
    assert outcome.results[1].severity is VendorSeverity.REQUIRED
    assert outcome.results[1].result == (("000001",),)
    assert outcome.results[1].duration_ms is not None
    assert outcome.status == "success"
    assert outcome.fallback_reason is None


def test_market_data_router_prefers_no_data_over_failures() -> None:
    def required_empty() -> tuple[tuple[str], ...]:
        return ()

    def required_fail() -> tuple[tuple[str], ...]:
        raise RuntimeError("offline")

    with pytest.raises(MarketDataNoDataError, match="insufficient rows") as exc_info:
        route(
            (
                VendorRoute("eastmoney", required_fail, VendorSeverity.REQUIRED),
                VendorRoute("sina", required_empty, VendorSeverity.REQUIRED),
            ),
            on_no_data="insufficient rows",
        )

    message = str(exc_info.value)
    assert "eastmoney: offline" in message
    assert "sina: insufficient rows" in message
    outcome = getattr(exc_info.value, "route_outcome", None)
    assert outcome is not None
    assert outcome.status == "no_data"
    assert outcome.fallback_reason == "no_data"
    assert [item.status for item in outcome.results] == ["failed", "no_data"]


def test_market_data_router_raises_market_data_failed_with_vendor_summary() -> None:
    def failing() -> tuple[tuple[str], ...]:
        raise RuntimeError("timeout")

    with pytest.raises(MarketDataFailedError, match=r"sina: .*timeout") as exc_info:
        route(
            (
                VendorRoute("eastmoney", failing, VendorSeverity.REQUIRED),
                VendorRoute("sina", failing, VendorSeverity.REQUIRED),
            )
        )

    assert str(exc_info.value).startswith("sina: ")
    assert str(exc_info.value).endswith("eastmoney: timeout; sina: timeout")
    outcome = getattr(exc_info.value, "route_outcome", None)
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.fallback_reason == "failed"
    assert [item.name for item in outcome.results] == ["eastmoney", "sina"]
    assert [item.status for item in outcome.results] == ["failed", "failed"]


def test_market_data_router_marks_circuit_open_as_skipped() -> None:
    def circuit_open() -> tuple[tuple[str], ...]:
        raise MarketDataFailedError("eastmoney", "circuit_open")

    outcome: RouteOutcome = route(
        (
            VendorRoute("eastmoney", circuit_open, VendorSeverity.REQUIRED),
            VendorRoute("sina", lambda: (("000001",),), VendorSeverity.REQUIRED),
        )
    )

    assert outcome.vendor == "sina"
    assert [item.name for item in outcome.results] == ["eastmoney", "sina"]
    assert [item.status for item in outcome.results] == ["skipped", "success"]
    assert [item.skipped for item in outcome.results] == [True, False]
    assert outcome.results[0].error == "circuit_open"
