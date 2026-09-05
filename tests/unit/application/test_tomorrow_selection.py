from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.unit.epoch_helpers import (
    candidate_field_values,
    coverage,
    daily_field_values,
    market_field_values,
    research_field_values,
)
from trader.application.ports.market import MarketDataPlaneSnapshot
from trader.application.recommendation.scored_selection import (
    ScoredSelectionNotReadyError,
    ScoredSelectionUseCase,
    assemble_scored_features,
)
from trader.domain.market.epochs import (
    CandidateFeatureRow,
    CandidateQuoteEpoch,
    DailyFeaturePack,
    DailyFeatureRow,
    MarketEpoch,
    ResearchEpoch,
)
from trader.domain.market.models import Board, Evidence, LiveQuote, MarketQuote
from trader.domain.market.research import (
    CorporateRiskCategory,
    CorporateRiskFact,
    ResearchObservation,
)
from trader.domain.recommendation.models import Strategy

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
        "corporate_risk_history_unavailable": 0.0,
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
    candidate = {
        "liquidity": 0.4,
        "trend": 0.3,
        "stability": 0.2,
        "data_completeness": 0.1,
    }
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
        board_candidate_weights={Strategy.TOMORROW: {board: candidate for board in boards}},
        board_local_strategy_weights={Strategy.TOMORROW: {board: local for board in boards}},
        selection=replace(
            recommendation_policy.selection,
            candidate_min_score=50.0,
            minimum_board_reliability=0.85,
            thresholds={**recommendation_policy.selection.thresholds, "tomorrow": 0.0},
        ),
    )


def test_use_case_assembles_one_coherent_epoch_and_applies_candidate_quote(
    recommendation_policy,
) -> None:
    snapshot = _data_snapshot()
    use_case = ScoredSelectionUseCase(_Reader(snapshot), _policy(recommendation_policy))
    assembled = assemble_scored_features(snapshot)

    result = use_case.execute(evaluated_at=NOW, max_age_seconds=60.0)
    evaluation = result.evaluations[0]

    assert evaluation.features.quote.price == 10.2
    assert evaluation.features.quote.high == 10.2
    assert evaluation.features.quote.source == "tencent"
    assert evaluation.features.quote.cross_source_deviation_pct == 0.2
    assert evaluation.features.quote.cross_source_verified is True
    assert evaluation.features.values["tail_return_30m"] == 91.0
    assert evaluation.features.values["entry_quality"] == 84.0
    assert evaluation.features.market_regime == "risk_on"
    assert evaluation.features.merge_epoch.startswith("market:")
    assert snapshot.candidate_quotes is not None
    assert snapshot.candidate_quotes.version in assembled[0].merge_epoch
    assert snapshot.candidate_quotes.version in evaluation.features.merge_epoch
    assert evaluation.disposition.value == "observe_only"
    assert tuple(flag.code for flag in evaluation.optional_flags) == ("board_data_reliability_below_threshold",)


def test_use_case_refuses_to_score_without_a_coherent_market_epoch(recommendation_policy) -> None:
    snapshot = _data_snapshot()
    reader = _Reader(MarketDataPlaneSnapshot(snapshot.daily_features, None, None, None))
    use_case = ScoredSelectionUseCase(reader, _policy(recommendation_policy))

    with pytest.raises(ScoredSelectionNotReadyError, match="coherent_market_epoch_unavailable"):
        use_case.execute(evaluated_at=NOW, max_age_seconds=60.0)

    current_use_case = ScoredSelectionUseCase(_Reader(snapshot), _policy(recommendation_policy))
    with pytest.raises(ScoredSelectionNotReadyError, match="market_epoch_from_future"):
        current_use_case.execute(evaluated_at=NOW - timedelta(seconds=1), max_age_seconds=60.0)

    assert snapshot.market is not None
    future_received_market = replace(snapshot.market, received_at=NOW + timedelta(seconds=1))
    future_received = replace(snapshot, market=future_received_market, candidate_quotes=None)
    with pytest.raises(ScoredSelectionNotReadyError, match="market_epoch_from_future"):
        ScoredSelectionUseCase(_Reader(future_received), _policy(recommendation_policy)).execute(
            evaluated_at=NOW,
            max_age_seconds=60.0,
        )


def test_feature_assembly_does_not_let_late_candidate_price_replace_newer_market_price() -> None:
    snapshot = _data_snapshot()
    assert snapshot.candidate_quotes is not None
    late_quote = replace(
        snapshot.candidate_quotes.quotes[0],
        source_time=NOW - timedelta(seconds=1),
    )
    candidate_epoch = replace(
        snapshot.candidate_quotes,
        quotes=(late_quote,),
        field_values={late_quote.code: candidate_field_values(late_quote)},
    )

    assembled = assemble_scored_features(replace(snapshot, candidate_quotes=candidate_epoch))

    assert assembled[0].quote.price == 10.0
    assert assembled[0].values["tail_return_30m"] == 70.0
    assert snapshot.market is not None
    assert assembled[0].merge_epoch == snapshot.market.version


def test_feature_assembly_applies_coherent_research_evidence_and_current_corporate_risk() -> None:
    snapshot = _data_snapshot()
    evidence = Evidence(
        evidence_id="official-risk",
        evidence_type="regulatory_filing",
        title="监管立案公告",
        source="exchange",
        published_at=NOW - timedelta(hours=1),
        received_at=NOW - timedelta(minutes=30),
        data_version="research-1",
    )
    research = ResearchEpoch(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-current",
        observations={
            "600001": ResearchObservation(
                corporate_risk_facts=(
                    CorporateRiskFact(
                        category=CorporateRiskCategory.OFFICIAL_INVESTIGATION,
                        announced_at=NOW - timedelta(hours=1),
                        evidence_id=evidence.evidence_id,
                        source="exchange_disclosure",
                    ),
                ),
                corporate_risk_history_complete=True,
                corporate_risk_registry_version="risk-1",
                evidence=(evidence,),
            )
        },
        source_versions={"exchange": "research-1"},
        field_values={
            "600001": research_field_values(
                source_time=NOW - timedelta(hours=1),
                received_time=NOW,
                data_version="research-1",
            )
        },
    )

    assembled = assemble_scored_features(replace(snapshot, research=research))

    assert assembled[0].values["official_investigation_history"] == 1.0
    assert assembled[0].values["corporate_risk_history_unavailable"] == 0.0
    assert evidence in assembled[0].evidence
    assert {item.evidence_type for item in assembled[0].evidence} == {
        "structured_point_in_time",
        "intraday_tail",
        "regulatory_filing",
    }
