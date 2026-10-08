"""Evaluate both terminal parents and publish immutable research conclusions."""

from typing import Protocol

from trader.training.application.cross_strategy_conclusion import (
    CrossStrategyConclusion,
    CrossStrategyConclusionService,
)
from trader.training.application.d25_terminal_holdout import D25TerminalHoldoutService
from trader.training.application.tomorrow_point_in_time_holdout import TomorrowPointInTimeHoldoutService
from trader.training.domain.evaluation.terminal_holdout import TerminalHoldoutParentState


class TerminalParentsPort(Protocol):
    def read(self) -> tuple[TerminalHoldoutParentState, TerminalHoldoutParentState]: ...


class TerminalConclusionPublicationPort(Protocol):
    def publish(self, conclusion: CrossStrategyConclusion) -> None: ...


class ExecuteTerminalHoldout:
    def __init__(self, parents: TerminalParentsPort, publisher: TerminalConclusionPublicationPort) -> None:
        self._parents = parents
        self._publisher = publisher

    def execute(self) -> CrossStrategyConclusion:
        tomorrow_parent, d25_parent = self._parents.read()
        tomorrow = TomorrowPointInTimeHoldoutService((), tomorrow_parent).execute()
        d25 = D25TerminalHoldoutService((), d25_parent).execute()
        conclusion = CrossStrategyConclusionService().execute(tomorrow, d25)
        self._publisher.publish(conclusion)
        return conclusion
