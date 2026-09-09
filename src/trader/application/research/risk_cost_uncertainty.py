"""Fail-closed orchestration for risk/cost/uncertainty and DeepSeek research."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from trader.domain.research.risk_cost_uncertainty import (
    RiskCostUncertaintyReport,
    RiskCostUncertaintySample,
    evaluate_risk_cost_uncertainty,
    insufficient_risk_cost_uncertainty_report,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class RiskCostUncertaintyPrerequisite:
    status: Literal["historical_data_insufficient", "historical_rejected", "historical_validated"]
    parent_report_hash: str
    model_artifact_hash: str | None
    point_in_time_parity: bool
    failure_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {
            "historical_data_insufficient",
            "historical_rejected",
            "historical_validated",
        }:
            raise ValueError("risk-cost prerequisite status is invalid")
        if _SHA256.fullmatch(self.parent_report_hash) is None:
            raise ValueError("risk-cost prerequisite parent hash is invalid")
        if self.model_artifact_hash is not None and _SHA256.fullmatch(self.model_artifact_hash) is None:
            raise ValueError("risk-cost prerequisite model hash is invalid")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if any(_REASON.fullmatch(item) is None for item in reasons):
            raise ValueError("risk-cost prerequisite reasons are invalid")
        if self.status == "historical_validated":
            if self.model_artifact_hash is None or not self.point_in_time_parity or reasons:
                raise ValueError("validated risk-cost prerequisite is incomplete")
        elif not reasons or self.point_in_time_parity:
            raise ValueError("unavailable risk-cost prerequisite is inconsistent")
        object.__setattr__(self, "failure_reasons", reasons)


class RiskCostUncertaintyEvidencePort(Protocol):
    def load_samples(
        self,
        prerequisite: RiskCostUncertaintyPrerequisite,
    ) -> tuple[RiskCostUncertaintySample, ...] | None: ...


class RiskCostUncertaintyResearchBuilder:
    def __init__(self, evidence: RiskCostUncertaintyEvidencePort) -> None:
        self._evidence = evidence

    def build(self, prerequisite: RiskCostUncertaintyPrerequisite) -> RiskCostUncertaintyReport:
        if prerequisite.status != "historical_validated":
            return insufficient_risk_cost_uncertainty_report(
                prerequisite.parent_report_hash,
                prerequisite.model_artifact_hash,
                (*prerequisite.failure_reasons, "risk_cost_parent_evidence_unavailable"),
                rejected=prerequisite.status == "historical_rejected",
            )
        samples = self._evidence.load_samples(prerequisite)
        if samples is None:
            return insufficient_risk_cost_uncertainty_report(
                prerequisite.parent_report_hash,
                prerequisite.model_artifact_hash,
                ("risk_cost_evidence_unavailable",),
            )
        assert prerequisite.model_artifact_hash is not None
        try:
            return evaluate_risk_cost_uncertainty(
                prerequisite.parent_report_hash,
                prerequisite.model_artifact_hash,
                samples,
            )
        except ValueError:
            return insufficient_risk_cost_uncertainty_report(
                prerequisite.parent_report_hash,
                prerequisite.model_artifact_hash,
                ("risk_cost_evidence_invalid",),
            )


__all__ = [
    "RiskCostUncertaintyEvidencePort",
    "RiskCostUncertaintyPrerequisite",
    "RiskCostUncertaintyResearchBuilder",
]
