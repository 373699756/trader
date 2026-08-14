"""Immutable Score-R4 challenger replay manifests and same-stock pair rows."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.challengers import ChallengerVariantId
from trader.domain.research.historical import CostSettlementBasis, ResearchBoard

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HybridSource = Literal["control_copy", "existing_facts"]
SelectionStatus = Literal["selected", "no_decision"]
R4ReplayStatus = Literal["replayed", "exploratory"]


@dataclass(frozen=True)
class ChallengerCandidateOverride:
    code: str
    continuous_entry_score: float | None
    continuous_entry_status: Literal["not_enabled", "scored", "critical_missing"]
    coverage_shrunk_score: float | None
    active_set_expanded: bool
    selection_eligible: bool
    force_observe_only: bool
    observe_reasons: tuple[str, ...]
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _code(self.code)
        _optional_score(self.continuous_entry_score)
        _optional_score(self.coverage_shrunk_score)
        if self.continuous_entry_status == "scored" and self.continuous_entry_score is None:
            raise ValueError("scored continuous-entry override requires a score")
        if self.continuous_entry_status != "scored" and self.continuous_entry_score is not None:
            raise ValueError("disabled or missing continuous-entry override cannot carry a score")
        if self.active_set_expanded and not self.selection_eligible:
            raise ValueError("active-set expansion must make the candidate selection eligible")
        reasons = tuple(sorted(set(self.observe_reasons)))
        if self.force_observe_only != bool(reasons):
            raise ValueError("challenger observe override must match its reasons")
        object.__setattr__(self, "observe_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ChallengerReplaySelection:
    code: str
    production_rank: int | None
    local_rank: int | None
    hybrid_rank: int | None
    local_score: float
    hybrid_score: float
    hybrid_source: HybridSource

    def __post_init__(self) -> None:
        _code(self.code)
        for rank in (self.production_rank, self.local_rank, self.hybrid_rank):
            if rank is not None and not 1 <= rank <= 6:
                raise ValueError("Score-R4 ranks must identify Top6 items")
        _score(self.local_score)
        _score(self.hybrid_score)
        if self.hybrid_source == "control_copy" and self.hybrid_score != self.local_score:
            raise ValueError("Score-R4 hybrid control copy must preserve the local score")


@dataclass(frozen=True)
class ChallengerSameStockPair:
    code: str
    board: ResearchBoard
    production_rank: int | None
    local_rank: int | None
    hybrid_rank: int | None
    production_weight: float
    local_weight: float
    hybrid_weight: float
    local_score: float
    hybrid_score: float
    hybrid_source: HybridSource
    settlement: CostSettlementBasis
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _code(self.code)
        if self.board != self.settlement.board or self.code != self.settlement.code:
            raise ValueError("Score-R4 pair must use the matching R2 settlement basis")
        for rank, weight in (
            (self.production_rank, self.production_weight),
            (self.local_rank, self.local_weight),
            (self.hybrid_rank, self.hybrid_weight),
        ):
            if rank is not None and not 1 <= rank <= 6:
                raise ValueError("Score-R4 pair ranks must identify Top6 items")
            if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
                raise ValueError("Score-R4 pair weights must be finite and in [0, 1]")
            if (rank is None) != (weight == 0.0):
                raise ValueError("Score-R4 unselected pair sides must have zero weight")
        _score(self.local_score)
        _score(self.hybrid_score)
        if self.hybrid_source == "control_copy" and self.hybrid_score != self.local_score:
            raise ValueError("Score-R4 pair control copy must preserve the local score")
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ChallengerDayReplay:
    trade_date: date
    day_hash: str
    input_hash: str
    overrides: tuple[ChallengerCandidateOverride, ...]
    pairs: tuple[ChallengerSameStockPair, ...]
    local_status: SelectionStatus
    hybrid_status: SelectionStatus
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.day_hash, "Score-R4 day")
        _hash(self.input_hash, "Score-R4 input")
        overrides = tuple(sorted(self.overrides, key=lambda item: item.code))
        pairs = tuple(sorted(self.pairs, key=lambda item: item.code))
        if tuple(item.code for item in overrides) != tuple(item.code for item in pairs):
            raise ValueError("Score-R4 overrides and pair rows must cover the same stocks")
        if any(item.settlement.decision_date != self.trade_date for item in pairs):
            raise ValueError("Score-R4 pair settlement dates must match the replay day")
        _validate_status(self.local_status, tuple(item.local_rank for item in pairs), "local")
        _validate_status(self.hybrid_status, tuple(item.hybrid_rank for item in pairs), "hybrid")
        _validate_weight_sum(tuple(item.production_weight for item in pairs), "production")
        _validate_weight_sum(tuple(item.local_weight for item in pairs), "local")
        _validate_weight_sum(tuple(item.hybrid_weight for item in pairs), "hybrid")
        object.__setattr__(self, "overrides", overrides)
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ChallengerVariantReplay:
    variant_id: ChallengerVariantId
    variant_version: str
    parameter_manifest_hash: str
    days: tuple[ChallengerDayReplay, ...]
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.parameter_manifest_hash, "Score-R4 parameter manifest")
        expected_version = {
            "continuous_entry": "continuous_entry_v1",
            "coverage_shrink": "coverage_shrink_v1",
            "candidate_upper_bound": "candidate_upper_bound_v1",
            "heat_weak_structure": "heat_weak_structure_v1",
            "combined_v1": "combined_v1",
        }[self.variant_id]
        if self.variant_version != expected_version:
            raise ValueError("Score-R4 variant identity and version do not match")
        days = tuple(sorted(self.days, key=lambda item: item.trade_date))
        if len(days) > 40 or len({item.trade_date for item in days}) != len(days):
            raise ValueError("Score-R4 variant accepts at most 40 unique historical days")
        object.__setattr__(self, "days", days)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class ScoreR4ChallengerReport:
    status: R4ReplayStatus
    extraction_hash: str
    baseline_report_hash: str
    parameter_manifest_hash: str
    variants: tuple[ChallengerVariantReplay, ...]
    schema_version: str = "score_r4_challenger_replay_v1"
    deepseek_http_request_delta: Literal[0] = 0
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.extraction_hash, "Score-R4 extraction")
        _hash(self.baseline_report_hash, "Score-R4 baseline")
        _hash(self.parameter_manifest_hash, "Score-R4 parameter manifest")
        if self.schema_version != "score_r4_challenger_replay_v1" or self.deepseek_http_request_delta != 0:
            raise ValueError("Score-R4 report identity or DeepSeek isolation is invalid")
        expected_ids = (
            "continuous_entry",
            "coverage_shrink",
            "candidate_upper_bound",
            "heat_weak_structure",
            "combined_v1",
        )
        if tuple(item.variant_id for item in self.variants) != expected_ids:
            raise ValueError("Score-R4 report must contain the fixed five-variant family")
        if any(item.parameter_manifest_hash != self.parameter_manifest_hash for item in self.variants):
            raise ValueError("Score-R4 variants must bind the same frozen parameter manifest")
        day_counts = {len(item.days) for item in self.variants}
        if len(day_counts) != 1:
            raise ValueError("Score-R4 variants must replay the same historical days")
        day_identities = {
            tuple((day.trade_date, day.day_hash, tuple(pair.code for pair in day.pairs)) for day in item.days)
            for item in self.variants
        }
        if len(day_identities) != 1:
            raise ValueError("Score-R4 variants must bind identical day and same-stock identities")
        expected_status = "replayed" if day_counts == {40} else "exploratory"
        if self.status != expected_status:
            raise ValueError("Score-R4 status must match its historical-day evidence")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_status(status: SelectionStatus, ranks: tuple[int | None, ...], label: str) -> None:
    expected = "selected" if any(rank is not None for rank in ranks) else "no_decision"
    if status != expected:
        raise ValueError(f"Score-R4 {label} status must match its selection")


def _validate_weight_sum(weights: tuple[float, ...], label: str) -> None:
    total = math.fsum(weights)
    if total != 0.0 and not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"Score-R4 {label} selected weights must sum to one")


def _code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("research stock code must contain exactly six digits")


def _score(value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError("Score-R4 scores must be finite and in [0, 100]")


def _optional_score(value: float | None) -> None:
    if value is not None:
        _score(value)


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} identity must be SHA-256")


__all__ = [
    "ChallengerCandidateOverride",
    "ChallengerDayReplay",
    "ChallengerReplaySelection",
    "ChallengerSameStockPair",
    "ChallengerVariantReplay",
    "HybridSource",
    "R4ReplayStatus",
    "ScoreR4ChallengerReport",
]
