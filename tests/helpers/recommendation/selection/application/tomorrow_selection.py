from __future__ import annotations
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from tests.helpers.recommendation.epoch_helpers import candidate_field_values, coverage, daily_field_values, market_field_values
from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot
from trader.recommendation.domain.market.epochs import CandidateFeatureRow, CandidateQuoteEpoch, DailyFeaturePack, DailyFeatureRow, MarketEpoch
from trader.recommendation.domain.market.models import Board, LiveQuote, MarketQuote
from trader.recommendation.domain.publication.models import Strategy

SHANGHAI = ZoneInfo("Asia/Shanghai")

NOW = datetime(2026, 7, 28, 14, 40, tzinfo=SHANGHAI)


class _Reader:
    def __init__(self, snapshot: MarketDataPlaneSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> MarketDataPlaneSnapshot:
        return self._snapshot


def _quote() -> MarketQuote:
    return MarketQuote(
        code="600001",
        name="正常股票",
        price=10.0,
        previous_close=9.8,
        open_price=9.9,
        high=10.1,
        low=9.7,
        pct_change=2.0,
        change_5m=0.1,
        speed=0.2,
        volume_ratio=1.2,
        turnover_rate=2.5,
        amount=100_000_000.0,
        amplitude=4.0,
        market_cap=10_000_000_000.0,
        industry="industry",
        source="eastmoney",
        source_time=NOW,
        received_time=NOW,
        data_version="market-1",
        board=Board.MAIN,
        board_source="security_master",
        board_reliability="verified",
        listing_age_sessions=100,
    )


def _data_snapshot() -> MarketDataPlaneSnapshot:
    values = {
        "amount_median_20d": 80_000_000.0,
        "trend_score": 70.0,
        "volatility_20d": 2.0,
        "max_drawdown_20d": -5.0,
        "turnover_median_20d": 1.0,
        "tail_return_30m": 70.0,
        "tail_volume_ratio": 70.0,
        "close_location": 70.0,
        "ma20_60_position": 70.0,
        "ma_slope": 70.0,
        "breakout_20d": 70.0,
        "entry_quality": 70.0,
        "latest_disclosed_report_loss": 0.0,
        "financial_deterioration": 0.0,
        "major_shareholder_reduction": 0.0,
        "financial_fraud_history": 0.0,
        "official_investigation_history": 0.0,
        "major_illegal_history": 0.0,
        "fund_occupation_history": 0.0,
        "illegal_guarantee_history": 0.0,
        "forced_delisting_risk": 0.0,
        "unlock_risk": 0.0,
        "pledge_risk": 0.0,
        "corporate_risk_window_unavailable": 0.0,
    }
    daily = DailyFeaturePack(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-current",
        calendar_version="calendar-current",
        rows=(
            DailyFeatureRow(
                code="600001",
                values=values,
                history_sessions=60,
                data_as_of=date(2026, 7, 27),
                security_master_version="master-initial",
                history_version="history-current",
                field_values=daily_field_values(values, source_time=NOW - timedelta(days=1), received_time=NOW),
            ),
        ),
        source_versions={"history": "history-1"},
        coverage=coverage(("600001",)),
    )
    market_quote = _quote()
    market = MarketEpoch(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-current",
        daily_feature_pack_version=daily.version,
        quotes=(market_quote,),
        source_versions={"eastmoney": "market-1"},
        field_values={market_quote.code: market_field_values(market_quote)},
        market_regime="risk_on",
    )
    live_quote = LiveQuote(
        code="600001",
        price=10.2,
        pct_change=4.0,
        source="tencent",
        source_time=NOW,
        received_time=NOW,
        data_version="candidate-1",
        cross_source_deviation_pct=0.2,
        cross_source_verified=True,
    )
    candidate_values = {
        "tail_return_30m": 91.0,
        "tail_volume_ratio": 88.0,
        "close_location": 86.0,
        "entry_quality": 84.0,
    }
    candidate = CandidateQuoteEpoch(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-current",
        market_epoch_version=market.version,
        quotes=(live_quote,),
        field_values={live_quote.code: candidate_field_values(live_quote)},
        feature_rows=(
            CandidateFeatureRow(
                code="600001",
                values=candidate_values,
                field_values=daily_field_values(candidate_values, source_time=NOW, received_time=NOW),
            ),
        ),
        source_versions={"tencent": "candidate-1"},
    )
    return MarketDataPlaneSnapshot(daily, market, candidate, None)


def _policy(recommendation_policy):

    local = {
        "tail_structure": 0.2,
        "turnover_flow": 0.1,
        "trend": 0.2,
        "stability": 0.2,
        "market_state": 0.1,
        "entry_quality": 0.2,
    }
    boards = (Board.MAIN, Board.CHINEXT, Board.STAR)
    return replace(
        recommendation_policy,
        board_policy_version="tomorrow-policy",
        board_local_strategy_weights={Strategy.TOMORROW: {board: local for board in boards}},
        local_component_weights={
            Strategy.TOMORROW: {
                "tail_structure": {
                    "tail_return_30m": 0.35,
                    "tail_volume_ratio": 0.3,
                    "close_location": 0.35,
                },
                "turnover_flow": {
                    "turnover_shock_score": 0.35,
                    "amount_shock_score": 0.35,
                    "flow_confirmation_score": 0.3,
                },
                "trend": {"ma20_60_position": 0.375, "ma_slope": 0.375, "breakout_20d": 0.25},
                "stability": {"low_volatility_score": 0.5, "low_drawdown_score": 0.5},
            }
        },
        selection=replace(
            recommendation_policy.selection,
            minimum_board_reliability=0.85,
            thresholds={**recommendation_policy.selection.thresholds, "tomorrow": 0.0},
        ),
    )
