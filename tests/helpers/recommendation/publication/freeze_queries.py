from __future__ import annotations
from datetime import date, datetime
from zoneinfo import ZoneInfo
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord, FreezeItem, FreezeQuote, ScoredFreeze
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")

NOW = datetime(2026, 8, 11, 10, 30, tzinfo=SHANGHAI)

TRADE_DATE = NOW.date()


class _Clock:
    def now(self) -> datetime:
        return NOW


class _FreezeRecords:
    def __init__(self, record: CommittedFreezeRecord | None = None) -> None:
        self.record = record

    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None:
        if self.record is not None and self.record.strategy is strategy and self.record.trade_date == trade_date:
            return self.record
        return None

    def list_dates(
        self,
        strategy: Strategy,
        *,
        limit: int = 31,
        year: int | None = None,
        month: int | None = None,
    ) -> tuple[date, ...]:
        if self.record is not None and self.record.strategy is strategy:
            trade_date = self.record.trade_date
            if year is not None and month is not None and (trade_date.year != year or trade_date.month != month):
                return ()
            return (trade_date,)[:limit]
        return ()


def _item(
    code: str,
    action: RecommendationAction,
    *,
    rank: int,
    final_score: float,
    local_score: float | None = None,
) -> FreezeItem:
    local = final_score if local_score is None else local_score
    return FreezeItem(
        code,
        action,
        True,
        rank,
        final_score,
        local,
        final_score,
        (("local_score", local),),
        (),
        "threshold_met" if action is RecommendationAction.EXECUTABLE else "near_score_threshold",
        Board.MAIN,
        rank,
        f"样例{code}",
        "样例行业",
        FreezeQuote(
            code,
            10.0,
            1.0,
            100_000_000.0,
            1.0,
            10_000_000_000.0,
            "fixture",
            NOW,
            f"quote:{code}",
        ),
    )


def _freeze(*, trade_date: date = TRADE_DATE) -> ScoredFreeze:
    return ScoredFreeze(
        Strategy.TOMORROW,
        trade_date,
        1,
        datetime.combine(trade_date, NOW.timetz()),
        "local",
        None,
        (("market", "market:1"),),
        "config:1",
        "strategy:1",
        "fusion:1",
        (
            FreezeItem(
                "600000",
                RecommendationAction.EXECUTABLE,
                True,
                1,
                88.0,
                84.0,
                84.0,
                (("local_score", 84.0),),
                ("risk_example",),
                "threshold_met",
                Board.MAIN,
                1,
                "浦发银行",
                "银行",
                FreezeQuote(
                    "600000",
                    10.25,
                    2.5,
                    1_000_000_000.0,
                    0.8,
                    300_000_000_000.0,
                    "fixture",
                    datetime.combine(trade_date, NOW.timetz()),
                    "quote:1",
                ),
            ),
        ),
        (("hard_filter", 10),),
    )
