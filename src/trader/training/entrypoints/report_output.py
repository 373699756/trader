"""Explicit JSON delivery and exclusive per-command diagnostic files."""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from typing import TextIO

from trader.training.application.error_diagnostics import TrainingErrorDiagnostic


def ProjectTrainingError(error: TrainingErrorDiagnostic) -> dict[str, object]:
    return {"causes": [
        {"error_type": cause.error_type, "code": cause.code, "errno": cause.errno,
         "frames": [{"source": frame.source, "function": frame.function, "line": frame.line}
                    for frame in cause.frames]}
        for cause in error.causes
    ]}


def TrainingErrorSummary(error: TrainingErrorDiagnostic) -> str:
    parts = []
    for cause in error.causes:
        location = cause.frames[-1] if cause.frames else None
        parts.append(
            f"{cause.error_type} [{cause.code}]"
            + (f" errno={cause.errno}" if cause.errno is not None else "")
            + (f" {location.source}:{location.line} {location.function}" if location is not None else "")
        )
    return " <- ".join(parts)


class TrainingReportOutput:
    def __init__(self, *, json_output: bool = False, diagnostics: Path | None = None) -> None:
        self._json_output = json_output
        self._path = diagnostics
        self._file: TextIO | None = None
        self._first = True

    def __enter__(self) -> TrainingReportOutput:
        if self._path is not None:
            self._file = self._path.open("x", encoding="utf-8")
            try:
                self._file.write('{"reports":[\n')
                self._file.flush()
            except OSError:
                self._file.close()
                self._file = None
                raise
        return self

    def emit(self, payload: dict[str, object]) -> None:
        if self._file is not None:
            self._file.write(("" if self._first else ",\n") + json.dumps(payload, ensure_ascii=False, indent=2))
            self._file.flush()
            self._first = False
        if self._json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2))

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            try:
                self._file.write("\n]}\n")
            finally:
                self._file.close()
                self._file = None


__all__ = [
    "ProjectTrainingError",
    "TrainingErrorSummary",
    "TrainingReportOutput",
]
