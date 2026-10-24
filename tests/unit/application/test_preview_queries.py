from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from tests.unit.application.test_decision_queries import NOW, _decision, _Repository
from trader.http_api.response.decision_projection import SerializeDecisionView
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedDecisionDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedDecisionIndex
from trader.recommendation.domain.publication.decision_identity import CommittedDecisionRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext


@pytest.mark.parametrize("hours_after_start", (0, 1, 2, 26))
@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_preview_current_survives_close_and_next_day_without_history_reads(hours_after_start, strategy):
    started_at = NOW.replace(day=16, hour=14)
    context = PreviewContext("preview-query", NOW.date(), started_at)
    source = _decision()
    completed_items = tuple(
        replace(item, quote=replace(item.quote, source_time=context.market_as_of)) for item in source.items
    )
    decision = replace(
        source,
        strategy=strategy,
        trade_date=started_at.date(),
        observed_at=started_at,
        preview_context=context,
        items=completed_items,
    )
    index = UnifiedDecisionIndex()
    assert index.publish(decision, expected_version=None).accepted

    class Clock:
        def now(self) -> datetime:
            return started_at + timedelta(hours=hours_after_start)

    class History:
        def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
            pytest.fail("preview current must not read formal history")

        def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
            pytest.fail("preview current must not read history dates")

    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), History(), Clock())
    first = queries.current(strategy)
    second = queries.current(strategy)
    payload = SerializeDecisionView(first)

    assert first.status == "ready" and first.preview_context is context
    assert first.decision_version == second.decision_version == decision.version
    assert first.etag == second.etag
    assert payload["trade_date"] == started_at.date().isoformat()
    assert payload["run_mode"] == "preview"
    assert payload["market_trade_date"] == NOW.date().isoformat()
    assert payload["market_as_of"] == context.market_as_of.isoformat()
    assert payload["generated_at"] == started_at.isoformat()
    assert payload["frozen"] is False and payload["frozen_at"] is None


def test_formal_history_response_does_not_acquire_preview_fields():
    record = CommittedDecisionRecord(_decision(), NOW.replace(hour=15), "scheduled")

    class Clock:
        def now(self) -> datetime:
            return NOW.replace(day=16, hour=17)

    queries = UnifiedDecisionQueries(UnifiedDecisionIndex(), UnifiedDecisionDraftIndex(), _Repository(record), Clock())
    payload = SerializeDecisionView(queries.history(Strategy.TOMORROW, NOW.date()))

    assert payload["frozen"] is True
    assert {"run_mode", "market_trade_date", "market_as_of", "generated_at"}.isdisjoint(payload)
    assert record.decision.preview_context is None
