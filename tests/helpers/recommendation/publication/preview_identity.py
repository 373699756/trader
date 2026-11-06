from __future__ import annotations
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo
from tests.helpers.recommendation.publication.freeze_identity import freeze
from trader.recommendation.domain.publication.freeze_identity import ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.preview import PreviewContext

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
