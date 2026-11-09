"""Evaluate both terminal parents and publish immutable research conclusions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from dataclasses import replace
from typing import Protocol

from trader.training.application.resources import TrainingMemoryAdmission

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


class ResearchCancelledError(InterruptedError):
    """The research command was cancelled without reopening its partition."""


class TerminalPreparedInputs(Protocol):
    @property
    def memory_bytes(self) -> int: ...

    def open_once(self) -> tuple[tuple[TerminalHoldoutRow, ...], tuple[TerminalHoldoutRow, ...]]: ...


class TerminalHoldoutDataPort(Protocol):
    def inspect(self) -> TerminalHoldoutEvidence:
        """Read bounded metadata, without opening any outcomes."""
        ...

    def prepare(
        self, evidence: TerminalHoldoutEvidence, strategies: tuple[TerminalStrategy, ...]
    ) -> AbstractContextManager[TerminalPreparedInputs]:
        """Pin ordinary descriptors and sizes without reading outcome bytes."""
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
        memory_admission: TrainingMemoryAdmission,
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
        cancel_requested: Callable[[], bool],
        admission_timeout_seconds: float = 30.0,
    ) -> None:
        self._parents = parents
        self._data = data
        self._publisher = publisher
        self._plan = plan
        if not 0 < admission_timeout_seconds <= 60:
            raise ValueError("research_admission_timeout_invalid")
        self._memory = memory_admission
        self._now, self._monotonic, self._cancel = now, monotonic, cancel_requested
        self._admission_timeout = admission_timeout_seconds

    def _check_cancel(self) -> None:
        if self._cancel():
            raise ResearchCancelledError("research_cancelled")

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
            self._check_cancel()
            if any(parent.already_opened for parent in parents):
                raise TerminalHoldoutAlreadyOpenedError("terminal_holdout_already_opened")
            for strategy, parent in zip(ordered_strategies, parents, strict=True):
                if parent.ready:
                    self._plan.validate_terminal_parent(strategy, parent)
                    if parent.registration is None:
                        raise ValueError("terminal_registration_missing")
                    parent.registration.validate_before_read(self._now())
            evidence = self._data.inspect()
            evidence.validate(self._plan, parents)
            for partition in (evidence.tomorrow, evidence.d25):
                state = partition.state_registration
                if partition.strategy in strategies and state is not None and state.registered_at > self._now():
                    raise ValueError("terminal_state_registered_after_read")
            with self._data.prepare(evidence, strategies) as prepared:
                deadline = self._monotonic() + self._admission_timeout

                def heartbeat() -> None:
                    self._check_cancel()
                    if self._monotonic() >= deadline:
                        raise TimeoutError("research_memory_admission_timeout")

                with self._memory.reserve(prepared.memory_bytes, heartbeat):
                    self._check_cancel()
                    tomorrow_rows, d25_rows = prepared.open_once()
                    self._check_cancel()
                    return self._evaluate(tomorrow_rows, d25_rows, tomorrow_parent, d25_parent, evidence_hash=evidence.content_hash)
        return self._evaluate(tomorrow_rows, d25_rows, tomorrow_parent, d25_parent)

    def _evaluate(
        self, tomorrow_rows: tuple[TerminalHoldoutRow, ...], d25_rows: tuple[TerminalHoldoutRow, ...],
        tomorrow_parent: TerminalHoldoutParentState, d25_parent: TerminalHoldoutParentState,
        *, evidence_hash: str | None = None,
    ) -> CrossStrategyConclusion:
        tomorrow = TomorrowPointInTimeHoldoutService(tomorrow_rows, tomorrow_parent).execute()
        self._check_cancel()
        d25 = D25TerminalHoldoutService(d25_rows, d25_parent).execute()
        self._check_cancel()
        tomorrow = replace(tomorrow, evidence_hash=evidence_hash)
        d25 = replace(d25, evidence_hash=evidence_hash)
        conclusion = CrossStrategyConclusionService().execute(tomorrow, d25)
        self._publisher.publish(conclusion)
        return conclusion
