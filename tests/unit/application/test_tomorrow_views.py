from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.unit.domain.test_tomorrow_fusion import _evaluation, _request, _selection
from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.tomorrow_views import (
    TomorrowDecisionQueries,
    TomorrowLiveQuote,
    TomorrowQuoteOverlay,
    TomorrowQuoteOverlayIndex,
    TomorrowRuntimeTelemetry,
    TomorrowSourceTelemetry,
    TomorrowTelemetryUnavailableError,
)
from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    build_decision_anchors,
)
from trader.domain.recommendation.tomorrow_fusion import (
    DecisionEpoch,
    build_tomorrow_decision_epoch,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 28, 14, 45, tzinfo=SHANGHAI)


class _Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class _Repository:
    def __init__(self, frozen: TomorrowDecisionFreeze | None = None) -> None:
        self.frozen = frozen
        self.loads: list[date] = []

    def save_checkpoint(self, checkpoint) -> None:
        raise AssertionError("read-only query must not save checkpoints")

    def load_checkpoint(self, trade_date):
        raise AssertionError("read-only query must not load checkpoints")

    def consume_checkpoint(self, checkpoint_version, *, consumed_at) -> None:
        raise AssertionError("read-only query must not consume checkpoints")

    def commit_freeze(self, frozen) -> None:
        raise AssertionError("read-only query must not commit freezes")

    def load_frozen(self, trade_date: date) -> TomorrowDecisionFreeze | None:
        self.loads.append(trade_date)
        return self.frozen if self.frozen is not None and self.frozen.trade_date == trade_date else None


def test_current_view_is_bounded_and_applies_only_matching_quote_overlay() -> None:
    decision = _decision(12)
    index = CurrentDecisionIndex()
    assert index.publish(decision, expected_current_version=None).accepted
    quotes = TomorrowQuoteOverlayIndex(index)
    first = min((item for item in decision.entries if item.selected), key=lambda item: item.rank)
    overlay = TomorrowQuoteOverlay(
        decision_version=decision.version,
        version="quote:2",
        observed_at=NOW + timedelta(seconds=2),
        quotes=(
            TomorrowLiveQuote(
                code=first.code,
                price=10.8,
                pct_change=8.0,
                source="tencent",
                source_time=NOW + timedelta(seconds=1),
                data_version="tencent:2",
            ),
        ),
    )
    assert quotes.publish(overlay, expected_overlay_version=None).accepted
    queries = TomorrowDecisionQueries(index, _Repository(), _Clock(NOW + timedelta(seconds=3)), quotes=quotes)

    view = queries.current()

    assert view.status == "ready"
    assert 0 < len(view.items) <= 10
    assert tuple(item.rank for item in view.items) == tuple(range(1, len(view.items) + 1))
    patched = next(item for item in view.items if item.code == first.code)
    assert patched.current_price == 10.8
    assert patched.quote_version == "tencent:2"
    assert patched.anchor_to_now_pct == 8.0
    assert view.quote_version == "quote:2"
    assert view.etag.startswith('"tomorrow:')

    stale = replace(overlay, decision_version="decision:wrong", version="quote:3")
    assert quotes.publish(stale, expected_overlay_version="quote:2").reason == "decision_mismatch"
    assert queries.current().quote_version == "quote:2"


def test_quote_overlay_index_accepts_a_new_decision_with_a_fresh_cas_identity() -> None:
    index = CurrentDecisionIndex()
    first = _decision(2)
    assert index.publish(first, expected_current_version=None).accepted
    quotes = TomorrowQuoteOverlayIndex(index)
    first_overlay = TomorrowQuoteOverlay(
        decision_version=first.version,
        version="quote:first",
        observed_at=NOW,
        quotes=(),
    )
    assert quotes.publish(first_overlay, expected_overlay_version=None).accepted
    second = replace(_decision(3), sequence=first.sequence + 1)
    assert index.publish(second, expected_current_version=first.version).accepted
    second_overlay = TomorrowQuoteOverlay(
        decision_version=second.version,
        version="quote:second",
        observed_at=NOW + timedelta(seconds=1),
        quotes=(),
    )

    result = quotes.publish(second_overlay, expected_overlay_version=None)

    assert result.accepted is True
    assert quotes.latest(first.version) is None
    assert quotes.latest(second.version) == second_overlay


def test_quote_overlay_rejects_non_selected_and_cross_day_quotes() -> None:
    decision = _decision(2)
    index = CurrentDecisionIndex()
    index.publish(decision, expected_current_version=None)
    quotes = TomorrowQuoteOverlayIndex(index)
    non_selected = TomorrowQuoteOverlay(
        decision.version,
        "quote:outside",
        NOW,
        (TomorrowLiveQuote("000001", 10.0, 1.0, "fixture", NOW, "q:1"),),
    )
    cross_day = TomorrowQuoteOverlay(
        decision.version,
        "quote:cross-day",
        NOW + timedelta(days=1),
        (),
    )

    assert quotes.publish(non_selected, expected_overlay_version=None).reason == "quote_scope_mismatch"
    assert quotes.publish(cross_day, expected_overlay_version=None).reason == "trade_date_mismatch"
    with pytest.raises(ValueError, match="share its trade date"):
        TomorrowQuoteOverlay(
            decision.version,
            "quote:mixed-day",
            NOW,
            (
                TomorrowLiveQuote(
                    "600000",
                    10.0,
                    1.0,
                    "fixture",
                    NOW - timedelta(days=1),
                    "q:mixed",
                ),
            ),
        )


def test_history_reads_only_exact_formal_freeze_and_never_uses_current_index() -> None:
    current = _decision(3)
    historical = _decision(3, observed_at=NOW - timedelta(days=1))
    frozen = TomorrowDecisionFreeze(
        decision=historical,
        frozen_at=datetime(2026, 7, 27, 14, 50, tzinfo=SHANGHAI),
        freeze_kind="scheduled",
        anchors=build_decision_anchors(historical),
    )
    index = CurrentDecisionIndex()
    assert index.publish(current, expected_current_version=None).accepted
    repository = _Repository(frozen)
    queries = TomorrowDecisionQueries(index, repository, _Clock())

    view = queries.history(date(2026, 7, 27))
    missing = queries.history(date(2026, 7, 26))

    assert view.status == "ready"
    assert view.frozen is True
    assert view.trade_date == "2026-07-27"
    assert view.projection_version == historical.version
    assert missing.status == "not_ready"
    assert missing.items == ()
    assert repository.loads == [date(2026, 7, 27), date(2026, 7, 26)]


def test_status_separates_source_age_latency_budget_and_current_identity() -> None:
    decision = _decision(2)
    index = CurrentDecisionIndex()
    index.publish(decision, expected_current_version=None)
    telemetry = TomorrowRuntimeTelemetry(
        sources=(
            TomorrowSourceTelemetry(
                name="tencent",
                status="healthy",
                source_time=NOW - timedelta(seconds=4),
                received_at=NOW - timedelta(seconds=2),
            ),
        ),
        pipeline_latency_ms=840.0,
        publish_latency_ms=8.0,
        deepseek_limit=168,
        deepseek_used=36,
        deepseek_reserved=6,
        recent_failures=("candidate_timeout",),
    )
    queries = TomorrowDecisionQueries(
        index,
        _Repository(),
        _Clock(),
        telemetry=lambda: telemetry,
    )

    status = queries.status()

    assert status.decision_version == decision.version
    assert status.sources[0].source_age_seconds == 4.0
    assert status.sources[0].receive_age_seconds == 2.0
    assert status.deepseek_remaining == 126
    assert status.recent_failures == ("candidate_timeout",)


def test_status_degrades_without_blocking_when_runtime_telemetry_is_unavailable() -> None:
    decision = _decision(2)
    index = CurrentDecisionIndex()
    index.publish(decision, expected_current_version=None)

    def unavailable() -> TomorrowRuntimeTelemetry:
        raise TomorrowTelemetryUnavailableError("injected")

    status = TomorrowDecisionQueries(
        index,
        _Repository(),
        _Clock(),
        telemetry=unavailable,
    ).status()

    assert status.status == "degraded"
    assert status.recent_failures == ("runtime_telemetry_unavailable",)


def _decision(size: int, *, observed_at: datetime = NOW) -> DecisionEpoch:
    evaluations = tuple(
        replace(
            evaluation,
            features=replace(
                evaluation.features,
                observed_at=observed_at,
                quote=replace(
                    evaluation.features.quote,
                    source_time=observed_at,
                    received_time=observed_at,
                ),
            ),
        )
        for index in range(size)
        for evaluation in (_evaluation(index, local_score=96.0 - index),)
    )
    request = replace(
        _request(_selection(evaluations)),
        observed_at=observed_at,
        trade_date=observed_at.date(),
        sequence=size,
    )
    return build_tomorrow_decision_epoch(request)
