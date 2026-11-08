"""Bounded exception evidence without payloads, absolute paths or frame locals."""

from dataclasses import dataclass
import re
from types import TracebackType


@dataclass(frozen=True)
class TrainingErrorFrame:
    source: str
    function: str
    line: int


@dataclass(frozen=True)
class TrainingErrorCause:
    error_type: str
    code: str
    errno: int | None
    frames: tuple[TrainingErrorFrame, ...]


@dataclass(frozen=True)
class TrainingErrorDiagnostic:
    causes: tuple[TrainingErrorCause, ...]


def _frames(trace: TracebackType | None) -> tuple[TrainingErrorFrame, ...]:
    frames: list[TrainingErrorFrame] = []
    while trace is not None:
        filename = trace.tb_frame.f_code.co_filename.replace("\\", "/")
        # The installed wheel has no src/ prefix; retain the package-relative suffix in both layouts.
        source = filename.rpartition("/trader/")[2]
        if source and all(part not in {"", ".", ".."} for part in source.split("/")):
            frames.append(TrainingErrorFrame("trader/" + source, trace.tb_frame.f_code.co_name, trace.tb_lineno))
        trace = trace.tb_next
    return tuple(frames[-16:])


def CaptureTrainingError(exc: BaseException) -> TrainingErrorDiagnostic:
    causes: list[TrainingErrorCause] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(causes) < 8:
        seen.add(id(current))
        message = str(current)
        # Publish only conventional error identifiers, never arbitrary exception payloads.
        code = message if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", message) else "training_operation_failed"
        causes.append(TrainingErrorCause(
            type(current).__name__, code,
            current.errno if isinstance(current, OSError) else None, _frames(current.__traceback__),
        ))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return TrainingErrorDiagnostic(tuple(causes))


__all__ = [
    "CaptureTrainingError",
    "TrainingErrorCause",
    "TrainingErrorDiagnostic",
    "TrainingErrorFrame",
]
