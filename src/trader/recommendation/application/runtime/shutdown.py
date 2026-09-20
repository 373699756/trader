"""Process-wide shutdown deadline, reporting, and signal coordination."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import FrameType

SignalHandler = Callable[[int, FrameType | None], object] | int | None


@dataclass(frozen=True)
class ShutdownDeadline:
    started_at_monotonic: float
    timeout_seconds: float
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

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


def signal_exit_code(signum: int) -> int:
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
    ) -> None:
        self._timeout_seconds = max(0.0, timeout_seconds)
        self._on_first_signal = on_first_signal
        self._force_exit = force_exit
        self._monotonic = monotonic
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
            return signal_exit_code(self._first_signal) if self._first_signal is not None else 0

    def handle(self, signum: int, _frame: object | None = None) -> None:
        with self._lock:
            if self._deadline is not None:
                exit_code = signal_exit_code(self._first_signal or signum)
                force = True
                deadline = self._deadline
            else:
                deadline = ShutdownDeadline.start(self._timeout_seconds, monotonic=self._monotonic)
                self._deadline = deadline
                self._first_signal = signum
                exit_code = signal_exit_code(signum)
                force = False
        if force:
            self._force_exit(exit_code)
            return
        threading.Thread(
            target=self._force_when_expired,
            args=(deadline,),
            name="trader-shutdown-watchdog",
            daemon=True,
        ).start()
        self._on_first_signal(deadline)

    def mark_completed(self) -> None:
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
            self._force_exit(2)


def _supported_shutdown_signals() -> tuple[int, ...]:
    supported = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        supported.append(signal.SIGTERM)
    if hasattr(signal, "SIGBREAK"):
        supported.append(signal.SIGBREAK)
    return tuple(dict.fromkeys(int(item) for item in supported))


__all__ = [
    "ShutdownDeadline",
    "ShutdownReport",
    "ShutdownSignalController",
    "ShutdownStep",
    "signal_exit_code",
]
