"""Typed immutable report for daily ranking stability research."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.replay_models import canonical_hash
from trader.application.research.score_r6_models import ScoreR6Metrics
from trader.domain.research.score_r6_stability import ScoreR6StabilityCandidate

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ScoreR6StabilityReport:
    status: Literal["insufficient_coverage", "parent_mismatch", "historical_rejected", "diagnostic_passed"]
    research_identity: str
    research_spec_hash: str
    parent_report_hash: str
    parent_candidate_hash: str
    archive: HistoricalArchiveStatus
    archive_manifest: HistoricalArchiveManifest
    selected_candidate: ScoreR6StabilityCandidate | None
    training: ScoreR6Metrics
    diagnostic: ScoreR6Metrics
    parent_training: ScoreR6Metrics
    parent_diagnostic: ScoreR6Metrics
    proxy_diagnostic: ScoreR6Metrics
    diagnostic_gate_passed: bool
    failure_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence_class: str
    promotion_authority: bool
    schema_version: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value in (self.research_spec_hash, self.parent_report_hash, self.parent_candidate_hash):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("daily stability report identity hash is invalid")
        if self.status == "diagnostic_passed" and not self.diagnostic_gate_passed:
            raise ValueError("daily stability pass status requires the diagnostic gate")
        if self.status != "diagnostic_passed" and self.diagnostic_gate_passed:
            raise ValueError("daily stability diagnostic gate requires pass status")
        if self.status in {"insufficient_coverage", "parent_mismatch"} and self.selected_candidate is not None:
            raise ValueError("daily stability prerequisite failure cannot freeze a candidate")
        if self.evidence_class != "reused_observed_validation_window":
            raise ValueError("daily stability report must disclose reused evidence")
        if self.promotion_authority:
            raise ValueError("daily stability diagnostic cannot promote production")
        if self.schema_version != "score_r6_daily_stability_report":
            raise ValueError("daily stability report schema is invalid")
        object.__setattr__(self, "failure_reasons", tuple(sorted(set(self.failure_reasons))))
        object.__setattr__(self, "limitations", tuple(sorted(set(self.limitations))))
        object.__setattr__(self, "content_hash", canonical_hash(self))


__all__ = ["ScoreR6StabilityReport"]
