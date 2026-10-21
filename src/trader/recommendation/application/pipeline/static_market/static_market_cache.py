"""Process-owned immutable startup eligibility, shared by matching profiles."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from trader.recommendation.application.pipeline.stage_output import PipelineStageOutput
from trader.recommendation.application.ports.market_data import MarketDataUnavailableError
from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.domain.market.eligibility import HistoricalStEligibilitySnapshot, IssuerEligibilityBatch
from trader.recommendation.domain.market.static import StaticIssuer


@dataclass(frozen=True, slots=True)
class StaticEligibilityInputs:
    reference: StaticReferenceRead
    historical_st: HistoricalStEligibilitySnapshot | None
    blacklist_codes: frozenset[str]


@dataclass(frozen=True, slots=True)
class StaticEligibilitySnapshot:
    inputs: StaticEligibilityInputs
    source: PipelineStageOutput[StaticIssuer]
    filtered: PipelineStageOutput[StaticIssuer]
    eligibility: IssuerEligibilityBatch
    eligible_codes: frozenset[str]


class StaticMarketCache:
    """Build once per startup input/filter identity; never rebuild on an online tick."""

    def __init__(self, *, reserve: Callable[[tuple[StaticEligibilitySnapshot, ...]], bool] | None = None) -> None:
        self._lock = threading.Lock()
        self._snapshots: tuple[StaticEligibilitySnapshot, ...] = ()
        self._reserve = reserve

    def initialize(
        self,
        inputs: StaticEligibilityInputs,
        build: Callable[[StaticEligibilityInputs], StaticEligibilitySnapshot],
    ) -> StaticEligibilitySnapshot:
        with self._lock:
            for snapshot in self._snapshots:
                if snapshot.inputs == inputs:
                    return snapshot
            snapshot = build(inputs)
            snapshots = (*self._snapshots, snapshot)
            if self._reserve is not None and not self._reserve(snapshots):
                raise MarketDataUnavailableError("static_eligibility_preload_capacity")
            self._snapshots = snapshots
            return snapshot


__all__ = ["StaticMarketCache", "StaticEligibilityInputs", "StaticEligibilitySnapshot"]
