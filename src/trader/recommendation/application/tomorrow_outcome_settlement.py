"""Background settlement for the absolute next-close target."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trader.recommendation.application.ports.outcomes import (
    TomorrowOutcomeEvidencePort,
    TomorrowSettlementMarketData,
)
from trader.recommendation.domain.outcomes.models import TomorrowRecommendationOutcome
from trader.recommendation.domain.outcomes.prediction_target import EvaluatePredictionTarget, ParsePredictionTargetIdentity
from trader.recommendation.domain.outcomes.tomorrow_target import TomorrowCloseGain


@dataclass(frozen=True, slots=True)
class TomorrowSettlementResult:
    targets: int
    settled: int


@dataclass(frozen=True, slots=True)
class TomorrowOutcomeSettlementService:
    market_data: TomorrowSettlementMarketData
    outcomes: TomorrowOutcomeEvidencePort
    target_limit: int = 500

    def settle(self, observed_at: datetime) -> TomorrowSettlementResult:
        targets = tuple(self.outcomes.pending_tomorrow_targets(limit=self.target_limit))
        if not targets:
            return TomorrowSettlementResult(0, 0)
        evidence = {
            (item.code, item.signal_date.isoformat()): item
            for item in self.market_data.read_tomorrow_close_evidence(targets, observed_at)
        }
        results: list[TomorrowRecommendationOutcome] = []
        for target in targets:
            item = evidence.get((target.stock_code, target.recommend_date))
            prediction = ParsePredictionTargetIdentity(target.prediction_target)
            if prediction is None or item is None or item.signal_raw_close != Decimal(str(target.anchor_raw_price)):
                continue
            label = EvaluatePredictionTarget(
                prediction,
                item.signal_close,
                (item.next_close,),
                confirmed_suspended=item.confirmed_suspended,
                next_raw_reference=item.next_raw_reference,
                next_raw_close=item.next_raw_close,
            )
            if label is not TomorrowCloseGain.PENDING:
                results.append(TomorrowRecommendationOutcome(target, item, label, observed_at))
        if results:
            self.outcomes.save_tomorrow_outcomes(results)
        return TomorrowSettlementResult(len(targets), len(results))
