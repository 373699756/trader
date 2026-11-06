"""Directed quote failure facts survive overlays without crossing Long's boundary."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.helpers.recommendation.market_data import NOW, StaticMarketClient, _quote
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.infra.market_data.gateway import MarketDataGateway


class _TargetReplies:
    def __init__(self, replies: Sequence[tuple[MarketQuote, ...] | RuntimeError]) -> None:
        self._replies = iter(replies)
        self.calls = 0

    def fetch_quotes(
        self,
        codes: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Sequence[MarketQuote]:
        del codes, timeout_seconds, cancel_event
        self.calls += 1
        reply = next(self._replies)
        if isinstance(reply, RuntimeError):
            raise reply
        return reply


def _gateway(
    quote: MarketQuote,
    target: _TargetReplies,
    wall_clock: Callable[[], datetime],
) -> MarketDataGateway:
    gateway = MarketDataGateway(
        StaticMarketClient((quote,)),
        StaticMarketClient((quote,)),
        target,
        minimum_market_rows=1,
        circuit_breaker_failures=10,
        circuit_breaker_seconds=60,
        wall_clock=wall_clock,
    )
    gateway.fetch_market(observed_at=NOW, force=True)
    return gateway


@pytest.mark.parametrize("outcome", ("future", "stale", "empty"))
def test_consecutive_target_failure_preserves_each_attempt_diagnosis_when_overlay_cannot_replace(outcome):
    quote = replace(_quote(), source="tencent", data_version="directed:baseline")
    failed_at = NOW + timedelta(seconds=2)
    second_quote = replace(
        quote,
        price=13.0,
        source_time=failed_at + timedelta(seconds=1) if outcome == "future" else NOW - timedelta(seconds=301),
        received_time=failed_at,
        data_version=f"directed:{outcome}",
    )
    target = _TargetReplies(((quote,), RuntimeError("controlled_first_failure"),
                             () if outcome == "empty" else (second_quote,)))
    current_time = [NOW]
    gateway = _gateway(quote, target, lambda: current_time[0])
    baseline = tuple(gateway.fetch_candidates((quote.code,), observed_at=NOW, force=True))
    current_time[0] = NOW + timedelta(seconds=1)
    first = tuple(gateway.fetch_candidates((quote.code,), observed_at=current_time[0], force=True))
    assert "market_data_degraded" in first[0].execution_restrictions
    current_time[0] = failed_at

    repeated = tuple(gateway.fetch_candidates((quote.code,), observed_at=failed_at, force=True))

    assert repeated == first
    assert replace(repeated[0], execution_restrictions=baseline[0].execution_restrictions) == baseline[0]
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None and snapshot.quotes == repeated
    expected_reason = {
        "future": "tencent:future_observation",
        "stale": f"freshness_expired:{quote.code}:tencent",
        "empty": "tencent:no_data",
    }[outcome]
    assert "tencent:source_failed" in snapshot.degraded_reasons
    assert expected_reason in snapshot.degraded_reasons
    assert snapshot.missing_reasons[f"{quote.code}.price.tencent"] == "targeted_quote_missing"
    assert target.calls == 3


@pytest.mark.parametrize("outcome", ("failure", "empty", "invalid"))
def test_long_failure_preserves_same_code_short_quote_and_later_same_identity_candidate_success(outcome):
    quote = replace(_quote(), source="tencent", data_version="shared:baseline")
    failed_at = NOW + timedelta(seconds=1)
    failure: tuple[MarketQuote, ...] | RuntimeError = {
        "failure": RuntimeError("controlled_long_failure"),
        "empty": (),
        "invalid": (replace(quote, price=None, source_time=failed_at, received_time=failed_at,
                            data_version="long:invalid"),),
    }[outcome]
    target = _TargetReplies(((quote,), failure, (quote,)))
    current_time = [NOW]
    gateway = _gateway(quote, target, lambda: current_time[0])
    baseline = tuple(gateway.fetch_candidates((quote.code,), observed_at=NOW, force=True))
    assert "market_data_degraded" not in baseline[0].execution_restrictions
    current_time[0] = failed_at

    retained = tuple(gateway.fetch_long_quotes((quote.code,), observed_at=failed_at, force=True))

    assert retained == baseline
    assert tuple(gateway.current_quotes((quote.code,))) == baseline
    snapshot = gateway.canonical_snapshot()
    assert snapshot is not None and snapshot.quotes == baseline
    expected_reason = {
        "failure": "tencent:source_failed",
        "empty": "tencent:no_data",
        "invalid": "tencent:partial_response",
    }[outcome]
    assert expected_reason in snapshot.degraded_reasons
    assert snapshot.missing_reasons[f"{quote.code}.price.tencent"] == "targeted_quote_missing"
    current_time[0] += timedelta(seconds=1)
    following = tuple(gateway.fetch_candidates((quote.code,), observed_at=current_time[0], force=True))
    assert following == baseline
    assert "market_data_degraded" not in following[0].execution_restrictions
    assert tuple(gateway.current_quotes((quote.code,))) == baseline
    assert target.calls == 3


def test_same_identity_target_cannot_clear_failure_restriction_but_new_observation_can():
    quote = replace(_quote(), source="tencent", data_version="guard:baseline")
    recovered_at = NOW + timedelta(seconds=3)
    recovered_quote = replace(
        quote, price=12.02, source_time=recovered_at, received_time=recovered_at, data_version="guard:recovered",
    )
    target = _TargetReplies(((quote,), RuntimeError("controlled_target_failure"), (quote,), (recovered_quote,)))
    current_time = [NOW]
    gateway = _gateway(quote, target, lambda: current_time[0])
    baseline = tuple(gateway.fetch_candidates((quote.code,), observed_at=NOW, force=True))
    current_time[0] += timedelta(seconds=1)
    failed = tuple(gateway.fetch_candidates((quote.code,), observed_at=current_time[0], force=True))
    assert "market_data_degraded" in failed[0].execution_restrictions
    current_time[0] += timedelta(seconds=1)

    same_identity = tuple(gateway.fetch_candidates((quote.code,), observed_at=current_time[0], force=True))

    assert same_identity == failed
    assert replace(same_identity[0], execution_restrictions=baseline[0].execution_restrictions) == baseline[0]
    current_time[0] = recovered_at
    recovered = tuple(gateway.fetch_candidates((quote.code,), observed_at=recovered_at, force=True))
    assert recovered[0].source_time == recovered[0].received_time == recovered_at
    assert recovered[0].price == 12.02
    assert recovered[0].data_version == recovered_quote.data_version
    assert "market_data_degraded" not in recovered[0].execution_restrictions
    assert tuple(gateway.current_quotes((quote.code,))) == recovered
    assert target.calls == 4
