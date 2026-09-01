"""Fail-closed CodexA terminal chain for unavailable H1 point-in-time evidence."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Literal

from trader.application.research.tomorrow_research_artifacts import (
    TomorrowResearchArtifactRef,
    TomorrowResearchStageHandoff,
)
from trader.domain.research.h1_point_in_time import (
    H1CapabilityAuditReport,
    H1Strategy,
    canonical_hash,
)
from trader.domain.research.historical_label import (
    H1CoverageMetadata,
    HistoricalLabelPreregistrationBatch,
    preregister_historical_labels,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HistoricalResidualLedgerTerminal:
    strategy: H1Strategy
    capability_hash: str
    parent_preregistration_hash: str
    status: Literal["historical_data_insufficient"]
    failure_reasons: tuple[str, ...]
    prediction_rows: int = 0
    outcome_rows: int = 0
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "historical_prediction_residual_ledger_terminal_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        if self.strategy not in {"today", "tomorrow", "d25"}:
            raise ValueError("historical residual terminal strategy is invalid")
        _hash(self.capability_hash, "residual capability")
        _hash(self.parent_preregistration_hash, "residual preregistration")
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.status != "historical_data_insufficient" or not reasons:
            raise ValueError("historical residual terminal requires bounded insufficient reasons")
        if self.prediction_rows or self.outcome_rows or self.terminal_holdout_opened or self.production_authority:
            raise ValueError("historical residual terminal cannot contain rows, open holdout, or authorize production")
        if self.schema_version != "historical_prediction_residual_ledger_terminal_v1":
            raise ValueError("historical residual terminal schema is invalid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class TomorrowC3Terminal:
    capability_hash: str
    parent_preregistration_hash: str
    parent_residual_ledger_hash: str
    status: Literal["historical_data_insufficient"]
    failure_reasons: tuple[str, ...]
    oof_artifact_hash: str | None = None
    candidate_model_artifact_hash: str | None = None
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    automatic_model_update: bool = False
    schema_version: str = "tomorrow_daily_close_c3_terminal_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.capability_hash, "C3 capability"),
            (self.parent_preregistration_hash, "C3 preregistration"),
            (self.parent_residual_ledger_hash, "C3 residual ledger"),
        ):
            _hash(value, label)
        reasons = tuple(sorted(set(self.failure_reasons)))
        if self.status != "historical_data_insufficient" or not reasons:
            raise ValueError("C3 terminal requires bounded insufficient reasons")
        if self.oof_artifact_hash is not None or self.candidate_model_artifact_hash is not None:
            raise ValueError("insufficient C3 evidence cannot claim OOF or a model artifact")
        if self.terminal_holdout_opened or self.production_authority or self.automatic_model_update:
            raise ValueError("C3 terminal cannot open holdout, authorize production, or update models")
        if self.schema_version != "tomorrow_daily_close_c3_terminal_v1":
            raise ValueError("C3 terminal schema is invalid")
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class CodexAResearchCompletion:
    capability_hash: str
    labels: HistoricalLabelPreregistrationBatch
    residual_ledgers: tuple[HistoricalResidualLedgerTerminal, ...]
    c3: TomorrowC3Terminal
    status: Literal["historical_data_insufficient"] = "historical_data_insufficient"
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    automatic_model_update: bool = False
    schema_version: str = "codex_a_h1_research_completion_v1"
    content_hash: str = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        _hash(self.capability_hash, "CodexA capability")
        ledgers = tuple(
            sorted(self.residual_ledgers, key=lambda item: ("today", "tomorrow", "d25").index(item.strategy))
        )
        if tuple(item.strategy for item in ledgers) != ("today", "tomorrow", "d25"):
            raise ValueError("CodexA completion requires every residual ledger terminal")
        if any(item.capability_hash != self.capability_hash for item in ledgers):
            raise ValueError("CodexA completion residual capability parent mismatch")
        tomorrow_label = next(item for item in self.labels.strategies if item.strategy == "tomorrow")
        tomorrow_ledger = next(item for item in ledgers if item.strategy == "tomorrow")
        if (
            self.c3.capability_hash != self.capability_hash
            or self.c3.parent_preregistration_hash != tomorrow_label.content_hash
            or self.c3.parent_residual_ledger_hash != tomorrow_ledger.content_hash
        ):
            raise ValueError("CodexA completion C3 parent mismatch")
        if self.status != "historical_data_insufficient":
            raise ValueError("CodexA insufficient completion status is invalid")
        if self.terminal_holdout_opened or self.production_authority or self.automatic_model_update:
            raise ValueError("CodexA completion cannot open holdout, authorize production, or update models")
        if self.schema_version != "codex_a_h1_research_completion_v1":
            raise ValueError("CodexA completion schema is invalid")
        object.__setattr__(self, "residual_ledgers", ledgers)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    def to_development_handoff(
        self,
        *,
        parent_graph_hash: str,
        resource_probe_artifact_hash: str,
    ) -> TomorrowResearchStageHandoff:
        _hash(parent_graph_hash, "Tomorrow research parent graph")
        _hash(resource_probe_artifact_hash, "Tomorrow resource probe")
        artifact = TomorrowResearchArtifactRef(
            artifact_id="h1_coverage_audit",
            artifact_kind="codex_a_h1_research_completion_v1",
            owner="codex_a",
            content_hash=self.content_hash,
            parent_hashes=(resource_probe_artifact_hash,),
            terminal_status="historical_data_insufficient",
            evidence_markers=("capability_audited", "terminal_holdout_not_opened"),
        )
        return TomorrowResearchStageHandoff(
            stage="development_training",
            parent_graph_hash=parent_graph_hash,
            artifacts=(artifact,),
            outcome="historical_data_insufficient",
            failure_reasons=("h1_historical_data_insufficient",),
        )


def complete_codex_a_research(
    *,
    capability: H1CapabilityAuditReport,
    metadata: tuple[H1CoverageMetadata, ...],
) -> CodexAResearchCompletion:
    if any(item.state != "historical_data_insufficient" for item in capability.strategies):
        raise ValueError("CodexA insufficient closure cannot replace a coverage-capable research execution")
    labels = preregister_historical_labels(metadata)
    if any(item.status != "historical_data_insufficient" for item in labels.strategies):
        raise ValueError("CodexA insufficient closure requires insufficient H1 metadata for every strategy")
    capability_by_strategy = {item.strategy: item for item in capability.strategies}
    ledgers = tuple(
        HistoricalResidualLedgerTerminal(
            strategy=item.strategy,
            capability_hash=capability.content_hash,
            parent_preregistration_hash=item.content_hash,
            status="historical_data_insufficient",
            failure_reasons=tuple(
                sorted(
                    set(
                        (
                            *item.failure_reasons,
                            *capability_by_strategy[item.strategy].failure_reasons,
                            *capability.probe_failures,
                        )
                    )
                )
            ),
        )
        for item in labels.strategies
    )
    tomorrow_label = next(item for item in labels.strategies if item.strategy == "tomorrow")
    tomorrow_ledger = next(item for item in ledgers if item.strategy == "tomorrow")
    c3 = TomorrowC3Terminal(
        capability_hash=capability.content_hash,
        parent_preregistration_hash=tomorrow_label.content_hash,
        parent_residual_ledger_hash=tomorrow_ledger.content_hash,
        status="historical_data_insufficient",
        failure_reasons=tomorrow_ledger.failure_reasons,
    )
    return CodexAResearchCompletion(capability.content_hash, labels, ledgers, c3)


def _hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} identity must be SHA-256")


__all__ = [
    "CodexAResearchCompletion",
    "HistoricalResidualLedgerTerminal",
    "TomorrowC3Terminal",
    "complete_codex_a_research",
]
