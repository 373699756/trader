"""V3 training application use case."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from trader.training.application.tomorrow_training import TomorrowTrainingProgressPort
from trader.training.application.training_ports import TrainingRunnerPort

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class TrainV3UseCase(Generic[_ResultT]):
    """Run only the injected V3 runner with its own profile contract."""

    runner: TrainingRunnerPort[_ResultT]

    def execute(
        self,
        history_root: Path,
        train_root: Path,
        *,
        progress: TomorrowTrainingProgressPort | None = None,
    ) -> _ResultT:
        return self.runner(history_root, train_root, progress=progress)


def train_v3(
    runner: TrainingRunnerPort[_ResultT],
    history_root: Path,
    train_root: Path,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> _ResultT:
    return TrainV3UseCase(runner).execute(
        history_root,
        train_root,
        progress=progress,
    )


__all__ = ["TrainV3UseCase", "train_v3"]
