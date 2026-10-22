"""Fail-closed application boundary for paired candidate and selection research."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from trader.training.domain.evaluation.research_identity import CanonicalResearchHash
from trader.training.domain.evaluation.selection_experiment import (
    EvaluatePairedSelectionExperiment,
    InsufficientPairedSelectionExperimentReport,
    PairedSelectionExperimentReport,
    PairedSelectionRow,
    PairedSelectionSpecification,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")


@dataclass(frozen=True)
class SelectionExperimentParentRowIdentity:
    trade_date: date
    code: str
    parent_row_hash: str

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit() or _HASH.fullmatch(self.parent_row_hash) is None:
            raise ValueError("selection experiment parent row identity is invalid")


@dataclass(frozen=True)
class SelectionExperimentPrerequisite:
    specification_hash: str
    qualification_hash: str
    dataset_status: Literal["historical_point_in_time_parity", "historical_data_insufficient"]
    candidate_recall_status: Literal["candidate_recall_attributed", "historical_data_insufficient"]
    dataset_hash: str
    dataset_manifest_hash: str
    candidate_recall_hash: str
    point_in_time_parity: bool
    terminal_holdout_opened: bool
    population_rows: tuple[SelectionExperimentParentRowIdentity, ...]
    failure_reasons: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        reasons = tuple(sorted(set(self.failure_reasons)))
        rows = tuple(sorted(self.population_rows, key=lambda item: (item.trade_date, item.code)))
        if self.dataset_status not in {"historical_point_in_time_parity", "historical_data_insufficient"}:
            raise ValueError("selection experiment dataset status is invalid")
        if self.candidate_recall_status not in {"candidate_recall_attributed", "historical_data_insufficient"}:
            raise ValueError("selection experiment candidate recall status is invalid")
        if any(
            _HASH.fullmatch(value) is None
            for value in (
                self.specification_hash,
                self.qualification_hash,
                self.dataset_hash,
                self.dataset_manifest_hash,
                self.candidate_recall_hash,
            )
        ):
            raise ValueError("selection experiment prerequisite hashes are invalid")
        if any(_REASON.fullmatch(value) is None for value in reasons):
            raise ValueError("selection experiment prerequisite reasons are invalid")
        ready = (
            self.dataset_status == "historical_point_in_time_parity"
            and self.candidate_recall_status == "candidate_recall_attributed"
            and self.point_in_time_parity
            and not self.terminal_holdout_opened
        )
        if ready == bool(reasons) or (ready and not rows) or (not ready and rows):
            raise ValueError("selection experiment prerequisite state and reasons are inconsistent")
        if len({(item.trade_date, item.code) for item in rows}) != len(rows):
            raise ValueError("selection experiment prerequisite parent rows must be unique")
        object.__setattr__(self, "population_rows", rows)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", CanonicalResearchHash(self))

    @property
    def ready(self) -> bool:
        return (
            self.dataset_status == "historical_point_in_time_parity"
            and self.candidate_recall_status == "candidate_recall_attributed"
            and self.point_in_time_parity
            and not self.terminal_holdout_opened
        )


class SelectionExperimentEvidencePort(Protocol):
    def load_rows(
        self,
        specification: PairedSelectionSpecification,
        prerequisite: SelectionExperimentPrerequisite,
    ) -> tuple[PairedSelectionRow, ...] | None: ...


class PairedSelectionExperimentBuilder:
    def __init__(self, evidence: SelectionExperimentEvidencePort) -> None:
        self._evidence = evidence

    def build(
        self,
        specification: PairedSelectionSpecification,
        prerequisite: SelectionExperimentPrerequisite,
    ) -> PairedSelectionExperimentReport:
        if (
            prerequisite.specification_hash != specification.content_hash
            or prerequisite.dataset_hash != specification.dataset_hash
            or prerequisite.dataset_manifest_hash != specification.dataset_manifest_hash
            or prerequisite.candidate_recall_hash != specification.candidate_recall_hash
        ):
            return InsufficientPairedSelectionExperimentReport(
                specification,
                ("selection_experiment_parent_mismatch",),
            )
        if not prerequisite.ready:
            return InsufficientPairedSelectionExperimentReport(
                specification,
                (*prerequisite.failure_reasons, "selection_experiment_prerequisite_unavailable"),
            )
        if tuple(sorted({item.trade_date for item in prerequisite.population_rows})) != specification.research_dates:
            return InsufficientPairedSelectionExperimentReport(
                specification,
                ("selection_experiment_parent_mismatch",),
            )
        try:
            rows = self._evidence.load_rows(specification, prerequisite)
            if rows is None:
                return InsufficientPairedSelectionExperimentReport(
                    specification,
                    ("selection_experiment_evidence_unavailable",),
                )
            expected = tuple(
                (item.trade_date, item.code, item.parent_row_hash) for item in prerequisite.population_rows
            )
            actual = tuple(sorted((item.trade_date, item.code, item.parent_row_hash) for item in rows))
            if actual != expected:
                raise ValueError("selection experiment evidence does not cover the exact parent rows")
            return EvaluatePairedSelectionExperiment(specification, rows)
        except ValueError:
            return InsufficientPairedSelectionExperimentReport(
                specification,
                ("selection_experiment_evidence_invalid",),
            )


__all__ = [
    "PairedSelectionExperimentBuilder",
    "SelectionExperimentEvidencePort",
    "SelectionExperimentParentRowIdentity",
    "SelectionExperimentPrerequisite",
]
