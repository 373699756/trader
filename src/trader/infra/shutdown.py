"""Process-wide shutdown deadline, reporting, and signal coordination."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import FrameType

SignalHandler = Callable[[int, FrameType | None], object] | int | None


@dataclass(frozen=True)
class ShutdownDeadline:
    started_at_monotonic: float
    timeout_seconds: float
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _progress: _ShutdownProgress = field(default_factory=lambda: _ShutdownProgress(), init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.timeout_seconds < 0.0:
            raise ValueError("shutdown timeout must be non-negative")

    @classmethod
    def start(
        cls,
        timeout_seconds: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ShutdownDeadline:
        return cls(
            started_at_monotonic=monotonic(),
            timeout_seconds=max(0.0, timeout_seconds),
            monotonic=monotonic,
        )

    def remaining_seconds(self) -> float:
        elapsed = max(0.0, self.monotonic() - self.started_at_monotonic)
        return max(0.0, self.timeout_seconds - elapsed)

    @property
    def expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.monotonic() - self.started_at_monotonic)


@dataclass(frozen=True)
class ShutdownStep:
    name: str
    completed: bool
    timed_out: bool
    cancelled_count: int = 0
    detail: str = ""


@dataclass(frozen=True)
class ShutdownSpan:
    name: str
    started_seconds: float
    finished_seconds: float | None = None


class _ShutdownProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: dict[int, ShutdownStep] = {}
        self._next_token = 0
        self._overflow = 0
        self._spans: dict[int, ShutdownSpan] = {}

    def begin(self, name: str, elapsed: float) -> int:
        with self._lock:
            if len(self._steps) >= 64:
                self._overflow += 1
                return 0
            self._next_token += 1
            self._steps[self._next_token] = ShutdownStep(
                name[:160], False, False, detail="shutdown step has started but not returned",
            )
            if len(self._spans) < 256:
                self._spans[self._next_token] = ShutdownSpan(name[:160], elapsed)
            return self._next_token

    def finish(self, token: int, elapsed: float) -> None:
        with self._lock:
            if token == 0:
                self._overflow -= 1
            else:
                self._steps.pop(token, None)
                span = self._spans.get(token)
                if span is not None:
                    self._spans[token] = replace(span, finished_seconds=elapsed)

    def timeline(self) -> tuple[ShutdownSpan, ...]:
        with self._lock:
            return tuple(self._spans.values())

    def snapshot(self, *, timed_out: bool) -> tuple[ShutdownStep, ...]:
        with self._lock:
            steps = tuple(replace(step, timed_out=timed_out) for step in self._steps.values())
            if self._overflow:
                steps = (*steps, ShutdownStep(
                    "shutdown:diagnostic_capacity", False, timed_out, detail="additional shutdown steps remain pending",
                ))
            return steps


@contextmanager
def ObserveShutdownStep(deadline: ShutdownDeadline, name: str) -> Iterator[None]:
    """Expose an unfinished owner through the existing process deadline."""
    token = deadline._progress.begin(name, deadline.elapsed_seconds)
    try:
        yield
    finally:
        deadline._progress.finish(token, deadline.elapsed_seconds)


def RecordShutdownEvent(deadline: ShutdownDeadline, name: str) -> None:
    elapsed = deadline.elapsed_seconds
    token = deadline._progress.begin(name, elapsed)
    deadline._progress.finish(token, elapsed)


def ReadShutdownTimeline(deadline: ShutdownDeadline) -> tuple[ShutdownSpan, ...]:
    return deadline._progress.timeline()


def PendingShutdownSteps(deadline: ShutdownDeadline) -> tuple[ShutdownStep, ...]:
    return deadline._progress.snapshot(timed_out=deadline.expired)


@dataclass(frozen=True)
class ShutdownReport:
    completed: bool
    forced: bool
    elapsed_seconds: float
    steps: tuple[ShutdownStep, ...]

    @classmethod
    def from_steps(
        cls,
        deadline: ShutdownDeadline,
        steps: Sequence[ShutdownStep],
        *,
        forced: bool = False,
    ) -> ShutdownReport:
        frozen_steps = tuple(steps)
        return cls(
            completed=not forced and bool(frozen_steps) and all(step.completed for step in frozen_steps),
            forced=forced,
            elapsed_seconds=deadline.elapsed_seconds,
            steps=frozen_steps,
        )


def SignalExitCode(signum: int) -> int:
    if signum == getattr(signal, "SIGTERM", 15):
        return 143
    return 130


class ShutdownSignalController:
    """Translate the first signal into graceful shutdown and the second into hard exit."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        on_first_signal: Callable[[ShutdownDeadline], None],
        force_exit: Callable[[int], object] = os._exit,
        monotonic: Callable[[], float] = time.monotonic,
        protect_process_exit: bool = False,
    ) -> None:
        self._timeout_seconds = max(0.0, timeout_seconds)
        self._on_first_signal = on_first_signal
        self._force_exit = force_exit
        self._monotonic = monotonic
        self._protect_process_exit = protect_process_exit
        self._lock = threading.RLock()
        self._deadline: ShutdownDeadline | None = None
        self._first_signal: int | None = None
        self._previous: dict[int, SignalHandler] = {}
        self._completed = threading.Event()

    @property
    def deadline(self) -> ShutdownDeadline | None:
        with self._lock:
            return self._deadline

    @property
    def exit_code(self) -> int:
        with self._lock:
            return SignalExitCode(self._first_signal) if self._first_signal is not None else 0

    def handle(self, signum: int, _frame: object | None = None) -> None:
        received = self._monotonic()
        with self._lock:
            if self._deadline is not None:
                exit_code = SignalExitCode(self._first_signal or signum)
                force = True
                deadline = self._deadline
            else:
                deadline = ShutdownDeadline(received, self._timeout_seconds, monotonic=self._monotonic)
                self._deadline = deadline
                self._first_signal = signum
                exit_code = SignalExitCode(signum)
                force = False
        if force:
            self._force_exit(exit_code)
            return
        token = deadline._progress.begin("signal_received", 0.0)
        deadline._progress.finish(token, 0.0)
        RecordShutdownEvent(deadline, "deadline_created")
        threading.Thread(
            target=self._force_when_expired,
            args=(deadline,),
            name="trader-shutdown-watchdog",
            daemon=True,
        ).start()
        RecordShutdownEvent(deadline, "watchdog_started")
        self._on_first_signal(deadline)

    def mark_completed(self) -> None:
        if self._deadline is not None:
            RecordShutdownEvent(self._deadline, "controller_completed")
        # A server's main function returning does not end the process: Python
        # still joins non-daemon threads and executor exit hooks. Keep its
        # original watchdog armed through that tail. Scoped CLI cancellation
        # continues to disarm the watchdog when its context has completed.
        if not self._protect_process_exit:
            self._completed.set()

    def install(self) -> None:
        for signum in _supported_shutdown_signals():
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self.handle)

    def restore(self) -> None:
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        self._previous.clear()

    def _force_when_expired(self, deadline: ShutdownDeadline) -> None:
        if not self._completed.wait(deadline.remaining_seconds()):
            # Logging may block on another thread's handler. The hard deadline
            # must not depend on diagnostics; the outer report owns that output.
            self._force_exit(2)


def _supported_shutdown_signals() -> tuple[int, ...]:
    supported = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        supported.append(signal.SIGTERM)
    if hasattr(signal, "SIGBREAK"):
        supported.append(signal.SIGBREAK)
    return tuple(dict.fromkeys(int(item) for item in supported))


__all__ = [
    "ObserveShutdownStep",
    "PendingShutdownSteps",
    "ReadShutdownTimeline",
    "RecordShutdownEvent",
    "ShutdownDeadline",
    "ShutdownReport",
    "ShutdownSignalController",
    "ShutdownSpan",
    "ShutdownStep",
    "SignalExitCode",
]
