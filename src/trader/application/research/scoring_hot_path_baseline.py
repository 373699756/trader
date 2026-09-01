"""Typed, production-isolated evidence for the scoring hot path."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from trader.domain.recommendation.models import Strategy

_IDENTITY = re.compile(r"^[a-zA-Z0-9_.:-]{1,200}$")
HotPathStatus = Literal["passed", "failed"]
EquivalenceStatus = Literal["passed", "failed"]


@dataclass(frozen=True)
class ScoringInputEpoch:
    """One completed scoring epoch and its typed dirty-set accounting."""

    strategy: Strategy
    phase: str
    version: str
    changed_codes: tuple[str, ...]
    evaluated_candidate_count: int
    recomputed_stock_count: int
    recomputed_factor_count: int
    external_request_count: int = 0
    cache_hit_count: int = 0
    cache_miss_count: int = 0
    sqlite_transaction_count: int = 0
    sqlite_bytes: int = 0
    latest_wins_replacement_count: int = 0
    cpu_time_ms: float = 0.0
    completed_before_freeze: bool = True

    def __post_init__(self) -> None:
        if not _IDENTITY.fullmatch(self.phase) or not _IDENTITY.fullmatch(self.version):
            raise ValueError("scoring epoch identities must be stable")
        codes = tuple(sorted(set(self.changed_codes)))
        if any(not re.fullmatch(r"\d{6}", code) for code in codes):
            raise ValueError("scoring epoch changed codes must be six digits")
        object.__setattr__(self, "changed_codes", codes)
        counts = (
            self.evaluated_candidate_count,
            self.recomputed_stock_count,
            self.recomputed_factor_count,
            self.external_request_count,
            self.cache_hit_count,
            self.cache_miss_count,
            self.sqlite_transaction_count,
            self.sqlite_bytes,
            self.latest_wins_replacement_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("scoring epoch counts must be non-negative")
        if not math.isfinite(self.cpu_time_ms) or self.cpu_time_ms < 0.0:
            raise ValueError("scoring epoch CPU time must be finite and non-negative")
        if self.recomputed_stock_count > self.evaluated_candidate_count:
            raise ValueError("recomputed stocks cannot exceed evaluated candidates")

    @property
    def changed_code_count(self) -> int:
        return len(self.changed_codes)


@dataclass(frozen=True)
class ScoringHotPathLatency:
    stage: str
    p50_ms: float | None
    p95_ms: float | None
    maximum_ms: float | None
    sample_count: int

    def __post_init__(self) -> None:
        if not self.stage or self.sample_count < 0:
            raise ValueError("latency stage and sample count are invalid")
        values = (self.p50_ms, self.p95_ms, self.maximum_ms)
        if any(value is not None and (not math.isfinite(value) or value < 0.0) for value in values):
            raise ValueError("latency values must be finite and non-negative")
        if self.sample_count == 0 and any(value is not None for value in values):
            raise ValueError("empty latency stage cannot expose values")


@dataclass(frozen=True)
class ScoringHotPathCost:
    """Independent resource denominators; zero candidates remain valid evidence."""

    completed_epoch_count: int
    evaluated_candidate_count: int
    formal_current_decision_count: int
    formal_frozen_decision_count: int
    deepseek_candidate_count: int
    recomputed_stock_count: int
    recomputed_factor_count: int
    external_request_count: int
    cache_hit_count: int
    cache_miss_count: int
    sqlite_transaction_count: int
    sqlite_bytes: int
    latest_wins_replacement_count: int
    cpu_time_ms: float

    def __post_init__(self) -> None:
        values = (
            self.completed_epoch_count,
            self.evaluated_candidate_count,
            self.formal_current_decision_count,
            self.formal_frozen_decision_count,
            self.deepseek_candidate_count,
            self.recomputed_stock_count,
            self.recomputed_factor_count,
            self.external_request_count,
            self.cache_hit_count,
            self.cache_miss_count,
            self.sqlite_transaction_count,
            self.sqlite_bytes,
            self.latest_wins_replacement_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("hot path cost counts must be non-negative")

    @property
    def cost_per_epoch(self) -> float:
        return self._ratio(self.external_request_count, self.completed_epoch_count)

    @property
    def cost_per_candidate(self) -> float:
        return self._ratio(self.external_request_count, self.evaluated_candidate_count)

    @property
    def cost_per_formal_decision(self) -> float:
        return self._ratio(
            self.external_request_count,
            self.formal_current_decision_count + self.formal_frozen_decision_count,
        )

    @property
    def cost_per_deepseek_candidate(self) -> float:
        return self._ratio(self.external_request_count, self.deepseek_candidate_count)

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 6) if denominator else 0.0


@dataclass(frozen=True)
class ScoringHotPathEquivalence:
    case: str
    status: EquivalenceStatus
    result_hash: str

    def __post_init__(self) -> None:
        if not _IDENTITY.fullmatch(self.case) or not re.fullmatch(r"[0-9a-f]{64}", self.result_hash):
            raise ValueError("equivalence evidence identity is invalid")


@dataclass(frozen=True)
class ScoringHotPathSlice:
    strategy: Strategy
    phase: str
    epochs: tuple[ScoringInputEpoch, ...]
    latencies: tuple[ScoringHotPathLatency, ...]
    cost: ScoringHotPathCost
    freeze_before_completion_rate: float
    recompute_shrink_ratio: float

    def __post_init__(self) -> None:
        if not _IDENTITY.fullmatch(self.phase) or not self.epochs:
            raise ValueError("hot path slice requires a phase and epoch")
        if not 0.0 <= self.freeze_before_completion_rate <= 1.0:
            raise ValueError("freeze completion rate must be within [0, 1]")
        if not 0.0 <= self.recompute_shrink_ratio <= 1.0:
            raise ValueError("recompute shrink ratio must be within [0, 1]")
        if any(epoch.strategy is not self.strategy or epoch.phase != self.phase for epoch in self.epochs):
            raise ValueError("slice epochs must share strategy and phase")
        object.__setattr__(self, "latencies", tuple(sorted(self.latencies, key=lambda item: item.stage)))


@dataclass(frozen=True)
class ScoringHotPathBaseline:
    slices: tuple[ScoringHotPathSlice, ...]
    equivalence: tuple[ScoringHotPathEquivalence, ...]
    relative_regression_percent: float
    allocation_growth_percent: float
    absolute_budget_passed: bool
    relative_budget_passed: bool
    allocation_budget_passed: bool
    schema_version: str = "scoring_hot_path_efficiency_baseline_v1"
    status: HotPathStatus = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "scoring_hot_path_efficiency_baseline_v1" or not self.slices or not self.equivalence:
            raise ValueError("scoring hot path baseline contract is invalid")
        if not math.isfinite(self.relative_regression_percent) or not math.isfinite(self.allocation_growth_percent):
            raise ValueError("baseline gate values must be finite")
        keys = tuple((item.strategy.value, item.phase) for item in self.slices)
        if len(set(keys)) != len(keys):
            raise ValueError("baseline slices must be unique")
        checks_passed = all(item.status == "passed" for item in self.equivalence)
        status: HotPathStatus = (
            "passed"
            if checks_passed
            and self.absolute_budget_passed
            and self.relative_budget_passed
            and self.allocation_budget_passed
            else "failed"
        )
        object.__setattr__(
            self, "slices", tuple(sorted(self.slices, key=lambda item: (item.strategy.value, item.phase)))
        )
        object.__setattr__(self, "equivalence", tuple(sorted(self.equivalence, key=lambda item: item.case)))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "content_hash", _hash_baseline(self))


def build_scoring_hot_path_baseline(  # noqa: PLR0913
    epochs: Iterable[ScoringInputEpoch],
    *,
    latencies: Iterable[ScoringHotPathLatency] = (),
    formal_current_decision_count: int = 0,
    formal_frozen_decision_count: int = 0,
    deepseek_candidate_count: int = 0,
    equivalence: Iterable[ScoringHotPathEquivalence] = (),
    relative_regression_percent: float = 0.0,
    allocation_growth_percent: float = 0.0,
    absolute_budget_passed: bool = True,
    relative_budget_passed: bool = True,
    allocation_budget_passed: bool = True,
) -> ScoringHotPathBaseline:
    epoch_values = tuple(epochs)
    if not epoch_values:
        raise ValueError("at least one scoring epoch is required")
    slices: list[ScoringHotPathSlice] = []
    for strategy, phase in sorted(
        {(item.strategy, item.phase) for item in epoch_values}, key=lambda item: item[0].value
    ):
        grouped = tuple(item for item in epoch_values if item.strategy is strategy and item.phase == phase)
        cost = ScoringHotPathCost(
            completed_epoch_count=len(grouped),
            evaluated_candidate_count=sum(item.evaluated_candidate_count for item in grouped),
            formal_current_decision_count=formal_current_decision_count if len(slices) == 0 else 0,
            formal_frozen_decision_count=formal_frozen_decision_count if len(slices) == 0 else 0,
            deepseek_candidate_count=deepseek_candidate_count if len(slices) == 0 else 0,
            recomputed_stock_count=sum(item.recomputed_stock_count for item in grouped),
            recomputed_factor_count=sum(item.recomputed_factor_count for item in grouped),
            external_request_count=sum(item.external_request_count for item in grouped),
            cache_hit_count=sum(item.cache_hit_count for item in grouped),
            cache_miss_count=sum(item.cache_miss_count for item in grouped),
            sqlite_transaction_count=sum(item.sqlite_transaction_count for item in grouped),
            sqlite_bytes=sum(item.sqlite_bytes for item in grouped),
            latest_wins_replacement_count=sum(item.latest_wins_replacement_count for item in grouped),
            cpu_time_ms=round(sum(item.cpu_time_ms for item in grouped), 3),
        )
        completed = sum(item.completed_before_freeze for item in grouped)
        evaluated = sum(item.evaluated_candidate_count for item in grouped)
        recomputed = sum(item.recomputed_stock_count for item in grouped)
        slices.append(
            ScoringHotPathSlice(
                strategy,
                phase,
                grouped,
                tuple(latencies),
                cost,
                completed / len(grouped),
                max(0.0, 1.0 - recomputed / evaluated) if evaluated else 0.0,
            )
        )
    return ScoringHotPathBaseline(
        tuple(slices),
        tuple(equivalence),
        relative_regression_percent,
        allocation_growth_percent,
        absolute_budget_passed,
        relative_budget_passed,
        allocation_budget_passed,
    )


def _hash_baseline(report: ScoringHotPathBaseline) -> str:
    payload = {
        "schema_version": report.schema_version,
        "status": report.status,
        "slices": [
            {
                "strategy": item.strategy.value,
                "phase": item.phase,
                "epochs": [
                    {
                        "version": epoch.version,
                        "changed_codes": epoch.changed_codes,
                        "evaluated_candidate_count": epoch.evaluated_candidate_count,
                        "recomputed_stock_count": epoch.recomputed_stock_count,
                        "recomputed_factor_count": epoch.recomputed_factor_count,
                        "cpu_time_ms": epoch.cpu_time_ms,
                    }
                    for epoch in item.epochs
                ],
                "cost": {
                    "completed_epoch_count": item.cost.completed_epoch_count,
                    "evaluated_candidate_count": item.cost.evaluated_candidate_count,
                    "formal_current_decision_count": item.cost.formal_current_decision_count,
                    "formal_frozen_decision_count": item.cost.formal_frozen_decision_count,
                    "deepseek_candidate_count": item.cost.deepseek_candidate_count,
                    "recomputed_stock_count": item.cost.recomputed_stock_count,
                    "recomputed_factor_count": item.cost.recomputed_factor_count,
                    "external_request_count": item.cost.external_request_count,
                    "cache_hit_count": item.cost.cache_hit_count,
                    "cache_miss_count": item.cost.cache_miss_count,
                    "sqlite_transaction_count": item.cost.sqlite_transaction_count,
                    "sqlite_bytes": item.cost.sqlite_bytes,
                    "latest_wins_replacement_count": item.cost.latest_wins_replacement_count,
                },
                "freeze_before_completion_rate": item.freeze_before_completion_rate,
                "recompute_shrink_ratio": item.recompute_shrink_ratio,
            }
            for item in report.slices
        ],
        "equivalence": [
            {"case": item.case, "status": item.status, "result_hash": item.result_hash} for item in report.equivalence
        ],
        "relative_regression_percent": report.relative_regression_percent,
        "allocation_growth_percent": report.allocation_growth_percent,
        "absolute_budget_passed": report.absolute_budget_passed,
        "relative_budget_passed": report.relative_budget_passed,
        "allocation_budget_passed": report.allocation_budget_passed,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = [
    "ScoringHotPathBaseline",
    "ScoringHotPathCost",
    "ScoringHotPathEquivalence",
    "ScoringHotPathLatency",
    "ScoringHotPathSlice",
    "ScoringInputEpoch",
    "build_scoring_hot_path_baseline",
]
