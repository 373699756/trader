"""Immutable inputs and evidence; JSON is decoded only by trace/report adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["passed", "failed", "unverified"]


@dataclass(frozen=True)
class FixtureFile:
    path: str
    content: str


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    prompt: str
    files: tuple[FixtureFile, ...]
    required_reads: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    behavior: str
    delivery: bool = False


@dataclass(frozen=True)
class CommandEvidence:
    command: str
    output: str
    exit_code: int


@dataclass(frozen=True)
class FinalClaim:
    summary: str
    verification: str
    live_verified: bool
    completed: bool


@dataclass(frozen=True)
class TraceEvidence:
    commands: tuple[CommandEvidence, ...] = ()
    messages: tuple[str, ...] = ()
    claim: FinalClaim | None = None
    complete: bool = False
    problems: tuple[str, ...] = ()
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    elapsed_seconds: float
    problem: str | None = None


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    evidence: str


@dataclass(frozen=True)
class CaseResult:
    name: str
    repetition: int
    process: ProcessResult
    trace: TraceEvidence
    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> Status:
        if self.process.exit_code != 0 or self.process.problem or self.trace.problems or not self.trace.complete:
            return "unverified"
        if any(check.status == "failed" for check in self.checks):
            return "failed"
        if any(check.status == "unverified" for check in self.checks):
            return "unverified"
        return "passed"
