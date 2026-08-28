"""Typed records and reports for preregistered Tomorrow shadow evidence."""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.application.research.replay_models import canonical_hash
from trader.domain.research.historical import SUPPORTED_RESEARCH_BOARDS, ResearchBoard
from trader.domain.research.paired_statistics import PreregisteredBootstrapResult, PreregisteredHolmDecision
from trader.domain.research.tomorrow_shadow_preregistration import (
    TOMORROW_SHADOW_CHALLENGER_FAMILY,
    TOMORROW_SHADOW_P1_SPEC,
    TomorrowShadowChallengerId,
)

ShadowEvidencePhase = Literal["historical", "forward"]
ShadowGateScope = Literal["historical", "forward", "combined"]
ShadowDayStatus = Literal["valid", "no_decision", "failed"]
ShadowVariantState = Literal["collecting", "passed", "rejected"]
ShadowGateState = Literal["collecting", "historical_passed", "forward_passed", "rejected", "promotion_eligible"]
ShadowProductionScope = Literal["none", "local_only", "hybrid"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class PreregisteredShadowPair:
    code: str
    board: ResearchBoard
    baseline_weight: float
    challenger_weight: float
    hybrid_weight: float
    gross_excess_return: float
    turnover: float
    mae_atr20: float
    score: float
    oracle_member: bool

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or self.board not in SUPPORTED_RESEARCH_BOARDS:
            raise ValueError("preregistered shadow pair security identity is invalid")
        numeric = (
            self.baseline_weight,
            self.challenger_weight,
            self.hybrid_weight,
            self.gross_excess_return,
            self.turnover,
            self.mae_atr20,
            self.score,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("preregistered shadow pair values must be finite")
        if any(not 0.0 <= value <= 1.0 for value in self.weights) or self.turnover < 0.0:
            raise ValueError("preregistered shadow weights or turnover are invalid")

    @property
    def weights(self) -> tuple[float, float, float]:
        return self.baseline_weight, self.challenger_weight, self.hybrid_weight


@dataclass(frozen=True)
class PreregisteredShadowDayRecord:
    research_spec_hash: str
    calendar_attestation_hash: str
    historical_gate_hash: str | None
    challenger_id: TomorrowShadowChallengerId
    phase: ShadowEvidencePhase
    planned_trade_date: date
    status: ShadowDayStatus
    feature_batch_hash: str | None
    shadow_report_hash: str | None
    selection_report_hash: str | None
    pairs: tuple[PreregisteredShadowPair, ...]
    failure_reason: str | None
    schema_version: str = "score_tomorrow_shadow_day_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_record_identity(self)
        pairs = tuple(sorted(self.pairs, key=lambda item: item.code))
        if len({item.code for item in pairs}) != len(pairs):
            raise ValueError("preregistered shadow pairs must contain unique codes")
        _validate_record_evidence(self, pairs)
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def lineage_hashes(self) -> tuple[str | None, str | None, str | None]:
        return self.feature_batch_hash, self.shadow_report_hash, self.selection_report_hash


def preregistered_shadow_evidence_manifest_hash(
    records: tuple[PreregisteredShadowDayRecord, ...],
) -> str:
    """Bind a gate report to the exact ordered immutable day evidence."""

    identities = tuple(
        {
            "challenger_id": record.challenger_id,
            "phase": record.phase,
            "planned_trade_date": record.planned_trade_date.isoformat(),
            "content_hash": record.content_hash,
        }
        for record in sorted(
            records,
            key=lambda item: (item.challenger_id, item.phase, item.planned_trade_date),
        )
    )
    return canonical_hash({"records": identities})


def _validate_record_identity(record: PreregisteredShadowDayRecord) -> None:
    spec = TOMORROW_SHADOW_P1_SPEC
    if record.research_spec_hash != spec.content_hash or _SHA256.fullmatch(record.calendar_attestation_hash) is None:
        raise ValueError("preregistered shadow record identity is invalid")
    if record.phase not in {"historical", "forward"} or record.status not in {"valid", "no_decision", "failed"}:
        raise ValueError("preregistered shadow record phase or status is invalid")
    if record.challenger_id not in TOMORROW_SHADOW_CHALLENGER_FAMILY:
        raise ValueError("preregistered shadow record challenger is invalid")
    if record.phase == "historical" and record.historical_gate_hash is not None:
        raise ValueError("historical shadow evidence cannot bind a historical gate")
    if record.phase == "forward" and (
        record.historical_gate_hash is None or _SHA256.fullmatch(record.historical_gate_hash) is None
    ):
        raise ValueError("forward shadow evidence must bind a historical gate")
    expected_dates = spec.historical_dates if record.phase == "historical" else spec.forward_dates
    if record.planned_trade_date not in expected_dates:
        raise ValueError("preregistered shadow record is outside its fixed phase window")
    if record.schema_version != "score_tomorrow_shadow_day_v1":
        raise ValueError("preregistered shadow record schema is invalid")


def _validate_record_evidence(
    record: PreregisteredShadowDayRecord,
    pairs: tuple[PreregisteredShadowPair, ...],
) -> None:
    if record.status == "failed":
        if pairs or record.failure_reason is None or _REASON.fullmatch(record.failure_reason) is None:
            raise ValueError("failed shadow record requires only a bounded reason")
        if any(value is not None for value in record.lineage_hashes):
            raise ValueError("failed shadow record cannot claim parent artifacts")
        return
    if not pairs or record.failure_reason is not None:
        raise ValueError("valid shadow record requires complete paired evidence")
    if any(value is None or _SHA256.fullmatch(value) is None for value in record.lineage_hashes):
        raise ValueError("valid shadow record requires all parent artifact hashes")
    _validate_weight_sums(pairs, record.status)


def _validate_weight_sums(pairs: tuple[PreregisteredShadowPair, ...], status: ShadowDayStatus) -> None:
    totals = tuple(math.fsum(weights) for weights in zip(*(item.weights for item in pairs), strict=True))
    if any(not (math.isclose(value, 0.0, abs_tol=1e-9) or math.isclose(value, 1.0, abs_tol=1e-9)) for value in totals):
        raise ValueError("preregistered shadow portfolio weights must sum to zero or one")
    if status == "no_decision" and (not math.isclose(totals[1], 0.0) or not math.isclose(totals[2], 0.0)):
        raise ValueError("no-decision shadow record must have zero challenger exposure")
    if status == "valid" and math.isclose(totals[1], 0.0):
        raise ValueError("valid shadow record requires challenger exposure")


@dataclass(frozen=True)
class PreregisteredShadowCostSensitivity:
    cost_rate: float
    mean_increment: float
    bootstrap: tuple[PreregisteredBootstrapResult, ...]

    def __post_init__(self) -> None:
        if self.cost_rate not in TOMORROW_SHADOW_P1_SPEC.cost_rates or not math.isfinite(self.mean_increment):
            raise ValueError("preregistered shadow cost sensitivity is invalid")
        if tuple(item.block_days for item in self.bootstrap) != TOMORROW_SHADOW_P1_SPEC.bootstrap_block_days:
            raise ValueError("preregistered shadow cost sensitivity requires every fixed block length")


@dataclass(frozen=True)
class PreregisteredShadowVariantGate:
    challenger_id: TomorrowShadowChallengerId
    state: ShadowVariantState
    day_count: int
    pair_count: int
    cost_sensitivities: tuple[PreregisteredShadowCostSensitivity, ...]
    baseline_severe_rate: float | None
    challenger_severe_rate: float | None
    turnover_increase: float | None
    oracle_recall: float | None
    delete_best_month_increment: float | None
    delete_best_board_increment: float | None
    maximum_stock_positive_fraction: float | None
    top_five_positive_fraction: float | None
    top_quintile_net_excess: float | None
    bottom_quintile_net_excess: float | None
    mean_rank_ic: float | None
    hybrid_primary_bootstrap: PreregisteredBootstrapResult | None
    production_scope: ShadowProductionScope
    failure_reasons: tuple[str, ...]
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.challenger_id not in TOMORROW_SHADOW_CHALLENGER_FAMILY or self.day_count < 0 or self.pair_count < 0:
            raise ValueError("preregistered shadow variant gate identity is invalid")
        if self.state not in {"collecting", "passed", "rejected"} or self.production_scope not in {
            "none",
            "local_only",
            "hybrid",
        }:
            raise ValueError("preregistered shadow variant gate state is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if any(_REASON.fullmatch(reason) is None for reason in reasons):
            raise ValueError("preregistered shadow gate reason is invalid")
        if self.state == "passed" and reasons:
            raise ValueError("passing shadow gate cannot carry failure reasons")
        if self.state in {"collecting", "rejected"} and not reasons:
            raise ValueError("non-passing shadow gate requires a reason")
        if self.state == "passed" and not self.cost_sensitivities:
            raise ValueError("passing shadow gate requires fixed cost sensitivities")
        if (
            self.cost_sensitivities
            and tuple(item.cost_rate for item in self.cost_sensitivities) != TOMORROW_SHADOW_P1_SPEC.cost_rates
        ):
            raise ValueError("preregistered shadow gate requires every fixed cost sensitivity")
        values = (
            self.baseline_severe_rate,
            self.challenger_severe_rate,
            self.turnover_increase,
            self.oracle_recall,
            self.delete_best_month_increment,
            self.delete_best_board_increment,
            self.maximum_stock_positive_fraction,
            self.top_five_positive_fraction,
            self.top_quintile_net_excess,
            self.bottom_quintile_net_excess,
            self.mean_rank_ic,
        )
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("preregistered shadow gate metrics must be finite")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def mean_increment_20bp(self) -> float | None:
        return self.cost_sensitivities[0].mean_increment if self.cost_sensitivities else None

    @property
    def bootstrap(self) -> tuple[PreregisteredBootstrapResult, ...]:
        return self.cost_sensitivities[0].bootstrap if self.cost_sensitivities else ()


@dataclass(frozen=True)
class PreregisteredShadowGateReport:
    research_spec_hash: str
    calendar_attestation_hash: str
    evidence_manifest_hash: str
    historical_report_hash: str | None
    scope: ShadowGateScope
    state: ShadowGateState
    variants: tuple[PreregisteredShadowVariantGate, ...]
    holm: tuple[PreregisteredHolmDecision, ...]
    production_authority: bool = False
    schema_version: str = "score_tomorrow_shadow_gate_report_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _validate_gate_report_identity(self)
        if self.state != _expected_gate_report_state(self):
            raise ValueError("preregistered shadow gate report state is inconsistent")
        object.__setattr__(self, "content_hash", canonical_hash(self))


def _validate_gate_report_identity(report: PreregisteredShadowGateReport) -> None:
    if report.research_spec_hash != TOMORROW_SHADOW_P1_SPEC.content_hash:
        raise ValueError("preregistered shadow gate report spec is invalid")
    if (
        _SHA256.fullmatch(report.calendar_attestation_hash) is None
        or _SHA256.fullmatch(report.evidence_manifest_hash) is None
    ):
        raise ValueError("preregistered shadow gate report evidence identity is invalid")
    if report.scope not in {"historical", "forward", "combined"} or report.state not in {
        "collecting",
        "historical_passed",
        "forward_passed",
        "rejected",
        "promotion_eligible",
    }:
        raise ValueError("preregistered shadow gate report scope or state is invalid")
    if report.scope == "historical" and report.historical_report_hash is not None:
        raise ValueError("historical shadow report cannot bind itself as a parent")
    if report.scope != "historical" and (
        report.historical_report_hash is None or _SHA256.fullmatch(report.historical_report_hash) is None
    ):
        raise ValueError("forward shadow report must bind its historical report")
    if tuple(item.challenger_id for item in report.variants) != TOMORROW_SHADOW_CHALLENGER_FAMILY:
        raise ValueError("preregistered shadow gate report requires the fixed family")
    if {item.challenger_id for item in report.holm} != set(TOMORROW_SHADOW_CHALLENGER_FAMILY):
        raise ValueError("preregistered shadow gate report requires all Holm members")
    if report.production_authority or report.schema_version != "score_tomorrow_shadow_gate_report_v1":
        raise ValueError("preregistered shadow gate report cannot authorize production")


def _expected_gate_report_state(report: PreregisteredShadowGateReport) -> ShadowGateState:
    if any(item.state == "collecting" for item in report.variants):
        return "collecting"
    if not any(item.state == "passed" for item in report.variants):
        return "rejected"
    if report.scope == "historical":
        return "historical_passed"
    if report.scope == "forward":
        return "forward_passed"
    return "promotion_eligible"


__all__ = [
    "PreregisteredShadowCostSensitivity",
    "PreregisteredShadowDayRecord",
    "PreregisteredShadowGateReport",
    "PreregisteredShadowPair",
    "PreregisteredShadowVariantGate",
    "ShadowDayStatus",
    "ShadowEvidencePhase",
    "ShadowGateScope",
    "preregistered_shadow_evidence_manifest_hash",
]
