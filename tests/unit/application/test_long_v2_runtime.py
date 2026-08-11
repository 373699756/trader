from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.long_groups import LongGroupDefinition, LongWatchItemDefinition
from trader.application.long_v2_runtime import LongV2Runtime, LongV2RuntimeDependencies
from trader.application.ports.long import LongRefreshRequest
from trader.application.ports.market import MarketDataUnavailableError
from trader.application.shutdown import ShutdownDeadline
from trader.domain.recommendation.decision_identity import LongProjection
from trader.domain.recommendation.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)


class _Quotes:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], datetime, bool]] = []

    def refresh_long_quotes(self, codes, observed_at, *, force=False, deadline=None):
        del deadline
        self.calls.append((tuple(codes), observed_at, force))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _runtime(
    quotes: _Quotes,
    *,
    completed_at: datetime = NOW,
) -> tuple[LongV2Runtime, UnifiedDecisionIndex]:
    index = UnifiedDecisionIndex()
    runtime = LongV2Runtime(
        LongV2RuntimeDependencies(quotes=quotes, index=index, now=lambda: completed_at),
        config_version="runtime-v2+strategy-v2",
        watchlist_version="long-watchlist-v2",
        items=(
            LongWatchItemDefinition("600001", "甲公司", "设备"),
            LongWatchItemDefinition("600002", "乙公司", "材料"),
            LongWatchItemDefinition("600003", "丙公司", "软件"),
        ),
        groups=(
            LongGroupDefinition("设备组", "chokepoint", ("600001", "600002")),
            LongGroupDefinition("软件组", "future_growth", ("600003",)),
        ),
    )
    return runtime, index


def test_long_v2_publishes_full_fixed_order_without_scoring(application_feature_factory) -> None:
    quotes = _Quotes(
        [
            (
                application_feature_factory("600001", NOW),
                application_feature_factory("600002", NOW),
                application_feature_factory("600003", NOW),
            )
        ]
    )
    runtime, index = _runtime(quotes)

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(NOW, "today_main"))
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.LONG).current

        assert isinstance(current, LongProjection)
        assert tuple(item.code for item in current.items) == ("600001", "600002", "600003")
        assert all(item.quote_status == "live" for item in current.items)
        assert all(item.price == 12.0 for item in current.items)
        assert len({item.group for item in current.items}) == 2
        assert index.snapshot(Strategy.LONG).formal is None
        assert runtime.status().score_status == "not_applicable"
        assert runtime.status().published_count == 1
        assert runtime.status().missing_count == 0
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_partial_refresh_retains_same_day_quote_and_keeps_missing_slot(
    application_feature_factory,
) -> None:
    first_at = NOW
    second_at = NOW + timedelta(seconds=1)
    quotes = _Quotes(
        [
            (application_feature_factory("600001", first_at), application_feature_factory("600002", first_at)),
            (application_feature_factory("600001", second_at),),
        ]
    )
    runtime, index = _runtime(quotes)

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(first_at, "today_main"))
        assert runtime.wait_idle(2.0)
        assert runtime.offer_refresh(LongRefreshRequest(second_at, "today_main"))
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.LONG).current

        assert isinstance(current, LongProjection)
        by_code = {item.code: item for item in current.items}
        assert by_code["600001"].quote_status == "live"
        assert by_code["600002"].quote_status == "retained"
        assert by_code["600002"].source_time == first_at
        assert by_code["600003"].quote_status == "missing"
        assert by_code["600003"].price is None
        assert tuple(item.code for item in current.items) == ("600001", "600002", "600003")
        assert runtime.status().degraded_reasons == ("long_quotes_partial",)
        assert runtime.status().retained_count == 1
        assert runtime.status().missing_count == 1
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_quote_failure_retains_current_and_does_not_replace_codes(application_feature_factory) -> None:
    quotes = _Quotes(
        [
            (application_feature_factory("600001", NOW),),
            MarketDataUnavailableError("injected"),
        ]
    )
    runtime, index = _runtime(quotes)

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(NOW, "today_main"))
        assert runtime.wait_idle(2.0)
        assert runtime.offer_refresh(LongRefreshRequest(NOW + timedelta(seconds=1), "today_main"))
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.LONG).current

        assert isinstance(current, LongProjection)
        assert tuple(item.code for item in current.items) == ("600001", "600002", "600003")
        assert current.items[0].quote_status == "retained"
        assert runtime.status().fetch_failure_count == 1
        assert runtime.status().degraded_reasons == ("long_quote_unavailable", "long_quotes_partial")
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_rejects_duplicate_or_unassigned_group_membership() -> None:
    quotes = _Quotes([()])
    item = LongWatchItemDefinition("600001", "甲公司", "设备")

    for groups in (
        (),
        (
            LongGroupDefinition("一组", "chokepoint", ("600001",)),
            LongGroupDefinition("二组", "future_growth", ("600001",)),
        ),
    ):
        try:
            LongV2Runtime(
                LongV2RuntimeDependencies(quotes=quotes, index=UnifiedDecisionIndex(), now=lambda: NOW),
                config_version="config-v2",
                watchlist_version="watchlist-v2",
                items=(item,),
                groups=groups,
            )
        except ValueError as exc:
            assert "exactly one long group" in str(exc)
        else:
            raise AssertionError("invalid long group ownership was accepted")


def test_long_v2_ignores_future_and_unknown_quotes(application_feature_factory) -> None:
    future = application_feature_factory("600001", NOW + timedelta(seconds=1))
    unknown = application_feature_factory("600999", NOW)
    runtime, index = _runtime(_Quotes([(future, unknown)]))

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(NOW, "today_main"))
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.LONG).current

        assert isinstance(current, LongProjection)
        assert all(item.quote_status == "missing" for item in current.items)
        assert runtime.status().missing_count == 3
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_accepts_quote_received_after_request_before_completion(application_feature_factory) -> None:
    feature = application_feature_factory("600001", NOW)
    feature = replace(feature, quote=replace(feature.quote, received_time=NOW + timedelta(seconds=1)))
    runtime, index = _runtime(_Quotes([(feature,)]), completed_at=NOW + timedelta(seconds=2))

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(NOW, "today_main"))
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.LONG).current

        assert isinstance(current, LongProjection)
        assert current.observed_at == NOW + timedelta(seconds=2)
        assert current.items[0].quote_status == "live"
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_rejects_stale_refresh_and_does_not_replace_newer_retained_quote(
    application_feature_factory,
) -> None:
    old_at = NOW
    new_at = NOW + timedelta(seconds=2)
    newer_quote = application_feature_factory("600001", new_at)
    older_quote = application_feature_factory("600001", old_at)
    runtime, index = _runtime(_Quotes([(newer_quote,), (older_quote,), (older_quote,)]))

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(new_at, "today_main"))
        assert runtime.wait_idle(2.0)
        assert runtime.offer_refresh(LongRefreshRequest(new_at + timedelta(seconds=1), "today_main"))
        assert runtime.wait_idle(2.0)
        retained = index.snapshot(Strategy.LONG).current
        assert isinstance(retained, LongProjection)
        assert retained.items[0].source_time == new_at
        assert retained.items[0].quote_status == "retained"

        assert runtime.offer_refresh(LongRefreshRequest(old_at, "today_main"))
        assert runtime.wait_idle(2.0)
        assert index.snapshot(Strategy.LONG).current == retained
        assert runtime.status().input_rejection_count == 1
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_clears_retained_quotes_on_the_next_trade_date(application_feature_factory) -> None:
    next_day = NOW + timedelta(days=1)
    runtime, index = _runtime(
        _Quotes(
            [
                (application_feature_factory("600001", NOW),),
                MarketDataUnavailableError("injected"),
            ]
        )
    )

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(NOW, "today_main"))
        assert runtime.wait_idle(2.0)
        assert runtime.offer_refresh(LongRefreshRequest(next_day, "today_main"))
        assert runtime.wait_idle(2.0)
        current = index.snapshot(Strategy.LONG).current

        assert isinstance(current, LongProjection)
        assert current.trade_date == next_day.date()
        assert all(item.quote_status == "missing" for item in current.items)
        assert runtime.status().retained_count == 0
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))


def test_long_v2_identity_is_independent_of_quote_return_order(application_feature_factory) -> None:
    first = application_feature_factory("600001", NOW)
    second = application_feature_factory("600002", NOW)
    first_runtime, first_index = _runtime(_Quotes([(first, second)]))
    second_runtime, second_index = _runtime(_Quotes([(second, first)]))

    for runtime in (first_runtime, second_runtime):
        runtime.start()
    try:
        request = LongRefreshRequest(NOW, "today_main")
        assert first_runtime.offer_refresh(request)
        assert second_runtime.offer_refresh(request)
        assert first_runtime.wait_idle(2.0)
        assert second_runtime.wait_idle(2.0)
    finally:
        first_runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))
        second_runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))

    assert first_index.snapshot(Strategy.LONG).current == second_index.snapshot(Strategy.LONG).current


def test_long_v2_rejects_conflicting_equal_identity_and_sanitizes_optional_values(
    application_feature_factory,
) -> None:
    first = application_feature_factory("600001", NOW)
    conflicting = replace(first, quote=replace(first.quote, price=13.0))
    partially_invalid = application_feature_factory("600002", NOW)
    partially_invalid = replace(
        partially_invalid,
        quote=replace(
            partially_invalid.quote,
            pct_change=float("nan"),
            amount=-1.0,
            turnover_rate=float("inf"),
            market_cap=-1.0,
        ),
    )
    invalid_price = application_feature_factory("600003", NOW)
    invalid_price = replace(invalid_price, quote=replace(invalid_price.quote, price=float("nan")))
    runtime, index = _runtime(_Quotes([(first, conflicting, partially_invalid, invalid_price)]))

    runtime.start()
    try:
        assert runtime.offer_refresh(LongRefreshRequest(NOW, "today_main"))
        assert runtime.wait_idle(2.0)
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(2.0))

    current = index.snapshot(Strategy.LONG).current
    assert isinstance(current, LongProjection)
    by_code = {item.code: item for item in current.items}
    assert by_code["600001"].quote_status == "missing"
    assert by_code["600002"].quote_status == "live"
    assert by_code["600002"].price == 12.0
    assert by_code["600002"].pct_change is None
    assert by_code["600002"].amount is None
    assert by_code["600002"].turnover_rate is None
    assert by_code["600002"].market_cap is None
    assert by_code["600003"].quote_status == "missing"
