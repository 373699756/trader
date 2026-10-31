"""Runtime-owned reusable HTTP processes; cancelled attempts are killed and reaped."""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import cast

from trader.infra.http_contracts import HttpCancelled, HttpError, HttpRequest, HttpResponse, HttpTimeout, HttpTransport
from trader.infra.shutdown import ShutdownDeadline, ShutdownStep


@dataclass(frozen=True)
class _HttpOutcome:
    response: HttpResponse | None
    failure: str = ""


def _serve(connection: Connection, transport_factory: Callable[[], HttpTransport]) -> None:
    try:
        with transport_factory() as transport:
            while True:
                request = cast(HttpRequest, connection.recv())
                try:
                    outcome = _HttpOutcome(transport.execute(request))
                except HttpTimeout:
                    outcome = _HttpOutcome(None, "timeout")
                except HttpError:
                    outcome = _HttpOutcome(None, "transport")
                connection.send(outcome)
    except (EOFError, BrokenPipeError):
        return
    finally:
        connection.close()


@dataclass(eq=False)
class _Worker:
    connection: Connection
    process: BaseProcess

    def dispose(self) -> None:
        self.connection.close()
        if self.process.pid is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=0.2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=1.0)
            self.process.close()


class IsolatedHttpExecutor:
    """Bound concurrency, reuse healthy connections, and discard timed-out workers."""

    def __init__(self, *, worker_count: int, transport_factory: Callable[[], HttpTransport]) -> None:
        if worker_count < 1:
            raise ValueError("http_worker_count_invalid")
        self._capacity = worker_count
        self._transport_factory = transport_factory
        self._condition = threading.Condition()
        self._idle: list[_Worker] = []
        self._active = 0
        self._running = False

    def start(self) -> bool:
        with self._condition:
            if self._running:
                return False
            if self._active or self._idle:
                raise RuntimeError("http_workers_still_stopping")
            self._running = True
            return True

    def _check(self, request: HttpRequest, deadline: float) -> None:
        if not self._running or (request.cancel_requested is not None and request.cancel_requested()):
            raise HttpCancelled("http_cancelled")
        if time.monotonic() >= deadline:
            raise HttpTimeout("http_deadline_exceeded")

    def _acquire(self, request: HttpRequest, deadline: float) -> _Worker | None:
        with self._condition:
            while True:
                self._check(request, deadline)
                if self._active < self._capacity:
                    self._active += 1
                    return self._idle.pop() if self._idle else None
                self._condition.wait(min(0.05, max(0.0, deadline - time.monotonic())))

    def _spawn(self) -> _Worker:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        worker = _Worker(parent, context.Process(target=_serve, args=(child, self._transport_factory), daemon=True))
        try:
            worker.process.start()
            return worker
        except BaseException:
            worker.dispose()
            raise
        finally:
            child.close()

    def execute(self, request: HttpRequest) -> HttpResponse:
        deadline = time.monotonic() + request.timeout_seconds
        if request.deadline is not None:
            deadline = min(deadline, request.deadline)
        worker = self._acquire(request, deadline)
        reusable = False
        try:
            worker = worker or self._spawn()
            self._check(request, deadline)
            worker.connection.send(replace(request, deadline=deadline, cancel_requested=None))
            while True:
                self._check(request, deadline)
                if worker.connection.poll(min(0.05, max(0.0, deadline - time.monotonic()))):
                    outcome = cast(_HttpOutcome, worker.connection.recv())
                    self._check(request, deadline)
                    if outcome.failure == "timeout":
                        raise HttpTimeout("http_timeout")
                    if outcome.response is None:
                        raise HttpError("http_transport_failed")
                    reusable = True
                    return outcome.response
                if not worker.process.is_alive():
                    raise HttpError("http_worker_exited")
        except HttpError:
            raise
        except (EOFError, OSError) as exc:
            raise HttpError("http_worker_failed") from exc
        finally:
            # Do not release capacity until a cancelled worker is fully reaped.
            with self._condition:
                if worker is not None and reusable and self._running:
                    self._idle.append(worker)
                    worker = None
            if worker is not None:
                worker.dispose()
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def stop(self, *, wait: bool = True, deadline: ShutdownDeadline | None = None) -> ShutdownStep:
        shared = deadline or ShutdownDeadline.start(5.0)
        with self._condition:
            self._running = False
            self._condition.notify_all()
            if wait:
                while self._active and not shared.expired:
                    self._condition.wait(min(0.05, shared.remaining_seconds()))
                for worker in self._idle:
                    worker.dispose()
                self._idle.clear()
            active = self._active
            completed = active == 0 and not self._idle
        return ShutdownStep("isolated_http", completed, bool(not completed and shared.expired),
                            detail=f"active={active}")


__all__ = ["IsolatedHttpExecutor"]
