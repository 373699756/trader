"""Immutable operation diagnostics kept separate from model quality and admission."""

from dataclasses import dataclass
import re
from typing import Literal

from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingProgressPort
from trader.training.application.error_diagnostics import CaptureTrainingError, TrainingErrorDiagnostic

TrainingIssueStage = Literal[
    "completion_notification", "sample_cleanup", "scope_cleanup", "outcome_notification",
]
_STAGES = {"completion_notification", "sample_cleanup", "scope_cleanup", "outcome_notification"}
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


@dataclass(frozen=True)
class TrainingOperationIssue:
    stage: TrainingIssueStage
    error_type: str
    cause_type: str | None = None
    diagnostic: TrainingErrorDiagnostic | None = None

    def __post_init__(self) -> None:
        if (
            self.stage not in _STAGES or _ERROR_TYPE.fullmatch(self.error_type) is None
            or (self.cause_type is not None and _ERROR_TYPE.fullmatch(self.cause_type) is None)
        ):
            raise ValueError("training_operation_issue_invalid")


def BuildTrainingOperationIssue(stage: TrainingIssueStage, exc: BaseException) -> TrainingOperationIssue:
    error_type = type(exc).__name__
    cause = exc.__cause__
    cause_type = type(cause).__name__ if cause is not None else None
    return TrainingOperationIssue(
        stage,
        error_type if _ERROR_TYPE.fullmatch(error_type) is not None else "Error",
        cause_type if cause_type is None or _ERROR_TYPE.fullmatch(cause_type) is not None else "Error",
        CaptureTrainingError(exc),
    )


def NotifyTrainingCommit(progress: TomorrowTrainingProgressPort | None) -> TrainingOperationIssue | None:
    if progress is None:
        return None
    try:
        progress.publish(TomorrowTrainingProgress("artifact_publish", "completed", 1, 1))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return BuildTrainingOperationIssue("completion_notification", exc)
    return None


__all__ = [
    "BuildTrainingOperationIssue", "NotifyTrainingCommit", "TrainingIssueStage", "TrainingOperationIssue",
]
