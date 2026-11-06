"""Immutable research parameters consumed before any evaluation rows are read."""

from __future__ import annotations

from dataclasses import dataclass

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.selection.execution_cost import ExecutionCostScenario
from trader.training.domain.evaluation.selection_experiment import SELECTION_ARMS, PairedSelectionSpecification
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState, TerminalStrategy


@dataclass(frozen=True)
class ResearchSettings:
    terminal_holdout_sessions: int
    tomorrow_embargo_sessions: int
    d25_embargo_sessions: int
    v2_fit_date_candidates: tuple[int, ...]
    v3_fit_date_candidates: tuple[int, ...]
    ablation_arms: tuple[str, ...]
    deepseek_comparison_arms: tuple[str, str]

    def __post_init__(self) -> None:
        if (
            type(self.terminal_holdout_sessions) is not int or self.terminal_holdout_sessions < 200
            or type(self.tomorrow_embargo_sessions) is not int or self.tomorrow_embargo_sessions < 5
            or type(self.d25_embargo_sessions) is not int or self.d25_embargo_sessions < 5
            or self.ablation_arms != SELECTION_ARMS
            or self.deepseek_comparison_arms != ("local_only", "structured_facts_veto")
        ):
            raise ValueError("research parameters do not meet the preregistered constraints")
        for candidates in (self.v2_fit_date_candidates, self.v3_fit_date_candidates):
            if (
                not candidates or candidates != tuple(sorted(set(candidates)))
                or any(type(value) is not int or value < 1 for value in candidates)
            ):
                raise ValueError("research fit dates must be sorted unique positive counts")


@dataclass(frozen=True)
class ResearchExecutionPlan:
    settings: ResearchSettings
    cost_scenarios: tuple[ExecutionCostScenario, ...]

    def __post_init__(self) -> None:
        expected = tuple(ExecutionCostScenario(f"cost_{bps}bp", bps / 10_000) for bps in (20, 50, 100))
        if self.cost_scenarios != expected:
            raise ValueError("research costs must preserve the preregistered scenarios")

    @property
    def configuration_hash(self) -> str:
        return CanonicalDigest(self.settings)

    @property
    def cost_reference_hash(self) -> str:
        return CanonicalDigest(self.cost_scenarios)

    @property
    def content_hash(self) -> str:
        return CanonicalDigest(self)

    def validate_selection(self, specification: PairedSelectionSpecification) -> None:
        settings, split = self.settings, specification.date_split
        embargo = settings.tomorrow_embargo_sessions if specification.strategy == "tomorrow" else settings.d25_embargo_sessions
        candidates = settings.v2_fit_date_candidates if specification.profile_id == "v2" else settings.v3_fit_date_candidates
        if (
            specification.configuration_hash != self.configuration_hash
            or specification.cost_reference_hash != self.cost_reference_hash
            or len(split.training_dates) not in candidates
            or len(split.development_confirmation_embargo_dates) < embargo
            or len(split.confirmation_holdout_embargo_dates) < embargo
            or len(split.terminal_holdout_dates) < settings.terminal_holdout_sessions
        ):
            raise ValueError("selection evidence does not match the configured research plan")

    def validate_terminal_parent(self, strategy: TerminalStrategy, parent: TerminalHoldoutParentState) -> None:
        split = parent.date_split
        embargo = (
            self.settings.tomorrow_embargo_sessions if strategy == "tomorrow"
            else self.settings.d25_embargo_sessions
        )
        if (
            split is None or split.terminal_holdout_opened or split.production_authority
            or len(split.development_confirmation_embargo_dates) < embargo
            or len(split.confirmation_holdout_embargo_dates) < embargo
            or len(split.terminal_holdout_dates) < self.settings.terminal_holdout_sessions
        ):
            raise ValueError("terminal parent has no valid sealed research date split")
