"""Application boundary for read-only training cadence decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from trader.application.training.training_ports import TrainingDuePort

_QueryT = TypeVar("_QueryT")
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class TrainingDueUseCase(Generic[_QueryT, _ResultT]):
    """Delegate due evaluation to a supplied policy without touching archives."""

    evaluator: TrainingDuePort[_QueryT, _ResultT]

    def execute(self, query: _QueryT) -> _ResultT:
        return self.evaluator(query)


__all__ = ["TrainingDueUseCase"]
