"""Settle D25 close-return events from frozen targets and published evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.recommendation.application.ports.outcomes import D25OutcomeEvidencePort, D25SettlementMarketData
from trader.recommendation.domain.outcomes.models import D25RecommendationOutcome
from trader.recommendation.domain.outcomes.prediction_target import EvaluatePredictionTarget, ParsePredictionTargetIdentity
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True, slots=True)
class D25SettlementResult:
    targets: int
    settled: int


@dataclass(frozen=True, slots=True)
class D25OutcomeSettlementService:
    market_data: D25SettlementMarketData
    outcomes: D25OutcomeEvidencePort
    target_limit: int = 500

    def settle(self, observed_at: datetime) -> D25SettlementResult:
        targets = tuple(self.outcomes.pending_d25_targets(limit=self.target_limit))
        if not targets:
            return D25SettlementResult(0, 0)
        evidence = {
            (item.code, item.signal_date.isoformat()): item
            for item in self.market_data.read_d25_close_evidence(targets, observed_at)
        }
        results: list[D25RecommendationOutcome] = []
        for target in targets:
            contract = ParsePredictionTargetIdentity(target.prediction_target)
            item = evidence.get((target.stock_code, target.recommend_date))
            if contract is None or contract.strategy is not Strategy.D25 or item is None:
                continue
            if item.signal_raw_close != Decimal(str(target.anchor_raw_price)):
                continue
            label = EvaluatePredictionTarget(contract, item.signal_close, item.closes)
            if label is not TomorrowCloseGain.PENDING:
                results.append(D25RecommendationOutcome(target, item, label, observed_at))
        if results:
            self.outcomes.save_d25_outcomes(results)
        return D25SettlementResult(len(targets), len(results))
