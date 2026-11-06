"""Evaluate both terminal parents and publish immutable research conclusions."""

from __future__ import annotations

from typing import Protocol

from trader.training.application.cross_strategy_conclusion import (
    CrossStrategyConclusion,
    CrossStrategyConclusionService,
)
from trader.training.application.d25_terminal_holdout import D25TerminalHoldoutService
from trader.training.application.tomorrow_point_in_time_holdout import TomorrowPointInTimeHoldoutService
from trader.training.domain.evaluation.research_plan import ResearchExecutionPlan
from trader.training.domain.evaluation.terminal_inputs import TerminalHoldoutEvidence
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState, TerminalHoldoutRow, TerminalStrategy


class TerminalHoldoutAlreadyOpenedError(RuntimeError):
    """A durable opening record forbids reading the same experiment again."""


class TerminalHoldoutDataPort(Protocol):
    def inspect(self) -> TerminalHoldoutEvidence:
        """Read bounded metadata, without opening any outcomes."""
        ...

    def open_once(
        self, evidence: TerminalHoldoutEvidence, strategies: tuple[TerminalStrategy, ...]
    ) -> tuple[tuple[TerminalHoldoutRow, ...], tuple[TerminalHoldoutRow, ...]]:
        """Persist an exclusive opening before reading; any failure consumes it."""
        ...


class TerminalParentsPort(Protocol):
    def read(self) -> tuple[TerminalHoldoutParentState, TerminalHoldoutParentState]: ...


class TerminalConclusionPublicationPort(Protocol):
    def publish(self, conclusion: CrossStrategyConclusion) -> None: ...


class ExecuteTerminalHoldout:
    def __init__(
        self,
        parents: TerminalParentsPort,
        data: TerminalHoldoutDataPort,
        publisher: TerminalConclusionPublicationPort,
        plan: ResearchExecutionPlan,
    ) -> None:
        self._parents = parents
        self._data = data
        self._publisher = publisher
        self._plan = plan

    def execute(self) -> CrossStrategyConclusion:
        tomorrow_parent, d25_parent = self._parents.read()
        parents = tomorrow_parent, d25_parent
        ordered_strategies: tuple[TerminalStrategy, ...] = ("tomorrow", "d25")
        strategies = tuple(
            strategy for strategy, parent in zip(ordered_strategies, parents, strict=True)
            if parent.ready
        )
        tomorrow_rows: tuple[TerminalHoldoutRow, ...] = ()
        d25_rows: tuple[TerminalHoldoutRow, ...] = ()
        if strategies:
            if any(parent.already_opened for parent in parents):
                raise TerminalHoldoutAlreadyOpenedError("terminal_holdout_already_opened")
            for strategy, parent in zip(ordered_strategies, parents, strict=True):
                if parent.ready:
                    self._plan.validate_terminal_parent(strategy, parent)
            evidence = self._data.inspect()
            evidence.validate(self._plan, parents)
            tomorrow_rows, d25_rows = self._data.open_once(evidence, strategies)
        tomorrow = TomorrowPointInTimeHoldoutService(tomorrow_rows, tomorrow_parent).execute()
        d25 = D25TerminalHoldoutService(d25_rows, d25_parent).execute()
        conclusion = CrossStrategyConclusionService().execute(tomorrow, d25)
        self._publisher.publish(conclusion)
        return conclusion
