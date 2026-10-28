from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.unit.domain.test_freeze_identity import freeze
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    FormalScoredFreeze,
    IsPreviewFreeze,
    IsShadowFreeze,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes, CommittedRecordFromBytes

SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_DATE = date(2026, 10, 23)
STARTED_AT = datetime(2026, 10, 24, 10, 0, tzinfo=SHANGHAI)


def preview_freeze(*, run_id: str = "preview-a", strategy: Strategy = Strategy.TOMORROW) -> ScoredFreeze:
    current = freeze(strategy)
    quote = current.items[0].quote
    assert quote is not None
    return replace(
        current,
        trade_date=STARTED_AT.date(),
        observed_at=STARTED_AT,
        items=(
            replace(current.items[0], quote=replace(quote, source_time=datetime(2026, 10, 23, 15, 1, tzinfo=SHANGHAI))),
        ),
        preview_context=PreviewContext(run_id, MARKET_DATE, STARTED_AT),
    )


@pytest.mark.parametrize("minute", (0, 1, 15))
def test_preview_accepts_actual_after_close_provider_times(minute: int) -> None:
    context = PreviewContext("session-a", MARKET_DATE, STARTED_AT)
    source = datetime(2026, 10, 23, 15, minute, tzinfo=SHANGHAI)
    assert context.market_as_of == datetime(2026, 10, 23, 15, 0, tzinfo=SHANGHAI)
    assert context.accepts_quote(source, STARTED_AT)
    assert context.accepts_quote(source.astimezone(timezone.utc), STARTED_AT)


@pytest.mark.parametrize(
    "source,observed",
    (
        (datetime(2026, 10, 23, 14, 59, tzinfo=SHANGHAI), STARTED_AT),
        (datetime(2026, 10, 22, 15, 0, tzinfo=SHANGHAI), STARTED_AT),
        (datetime(2026, 10, 24, 15, 0, tzinfo=SHANGHAI), STARTED_AT),
        (datetime(2026, 10, 23, 15, 1, tzinfo=SHANGHAI), datetime(2026, 10, 23, 15, 0, tzinfo=SHANGHAI)),
        (datetime(2026, 10, 23, 15, 0), STARTED_AT),
        (datetime(2026, 10, 23, 15, 0, tzinfo=SHANGHAI), STARTED_AT.replace(tzinfo=None)),
    ),
)
def test_preview_rejects_unclosed_wrong_session_future_or_naive_quotes(source: datetime, observed: datetime) -> None:
    assert not PreviewContext("session-a", MARKET_DATE, STARTED_AT).accepts_quote(source, observed)


def test_preview_context_normalizes_start_time_and_rejects_invalid_identity() -> None:
    context = PreviewContext("session-a", MARKET_DATE, STARTED_AT.astimezone(timezone.utc))
    assert context.started_at.tzinfo == SHANGHAI
    with pytest.raises(ValueError, match="identity"):
        PreviewContext("bad:identity", MARKET_DATE, STARTED_AT)
    with pytest.raises(ValueError, match="aware"):
        PreviewContext("session-a", MARKET_DATE, STARTED_AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="future"):
        PreviewContext("session-a", STARTED_AT.date() + timedelta(days=1), STARTED_AT)


def test_preview_uses_runtime_date_and_separate_market_session_and_run_namespace() -> None:
    first = preview_freeze()
    second = preview_freeze(run_id="preview-b")
    assert first.trade_date == STARTED_AT.date()
    assert first.preview_context is not None and first.preview_context.market_trade_date == MARKET_DATE
    assert ("market_trade_date", "2026-10-23") in first.input_versions
    assert ("run_mode", "preview") in first.input_versions
    assert IsPreviewFreeze(first) and not IsShadowFreeze(first)
    assert first.items[0].local_score == first.items[0].final_score == 88.0
    assert first.version != second.version and first.content_hash != second.content_hash
    assert replace(first, sequence=3).preview_context is first.preview_context


def test_preview_context_cannot_be_removed_forged_or_used_with_unclosed_quotes() -> None:
    current = preview_freeze()
    with pytest.raises(ValueError, match="typed run context"):
        replace(current, preview_context=None)
    with pytest.raises(ValueError, match="conflicts"):
        replace(current, preview_context=PreviewContext("other", MARKET_DATE, STARTED_AT))
    quote = current.items[0].quote
    assert quote is not None
    with pytest.raises(ValueError, match="completed market session"):
        replace(
            current,
            items=(replace(current.items[0], quote=replace(quote, source_time=quote.source_time.replace(hour=14))),),
        )


def test_preview_cannot_become_formal() -> None:
    current = preview_freeze()
    with pytest.raises(ValueError, match="preview"):
        FormalScoredFreeze(current)
    with pytest.raises(ValueError, match="preview"):
        CommittedFreezeRecord(current, STARTED_AT, "scheduled")
    with pytest.raises(ValueError, match="preview"):
        replace(current, is_formal=True)


def test_live_formal_codec_keeps_existing_shape_and_exact_round_trip() -> None:
    current = freeze()
    record = CommittedFreezeRecord(current, current.observed_at, "scheduled")
    encoded = CommittedRecordBytes(record)
    assert not IsPreviewFreeze(current)
    assert "preview_context" not in json.loads(encoded)["freeze"]
    assert CommittedRecordBytes(CommittedRecordFromBytes(encoded)) == encoded
