"""Ports for training commands and due-state evaluation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol, TypeVar

from trader.application.research.tomorrow_training import TomorrowTrainingProgressPort

_ResultT = TypeVar("_ResultT", covariant=True)
_QueryT = TypeVar("_QueryT", contravariant=True)


class TrainingRunnerPort(Protocol[_ResultT]):
    """Profile-specific runner supplied by infrastructure."""

    def __call__(
        self,
        history_root: Path,
        train_root: Path,
        *,
        progress: TomorrowTrainingProgressPort | None = None,
        observed_at: datetime | None = None,
    ) -> _ResultT: ...


class TrainingDuePort(Protocol[_QueryT, _ResultT]):
    """Read-only training cadence decision supplied by infrastructure."""

    def __call__(self, query: _QueryT) -> _ResultT: ...


__all__ = ["TrainingDuePort", "TrainingRunnerPort"]
