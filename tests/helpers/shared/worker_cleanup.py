"""Bound test-owned worker cleanup and retain incomplete shutdown evidence."""

from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import ManagedWorkerExecutor


def StopFixtureWorkers(pool: ManagedWorkerExecutor, *, timeout_seconds: float = 3.0) -> None:
    report = pool.stop(cancel_futures=True, deadline=ShutdownDeadline.start(timeout_seconds))
    assert report.completed, f"fixture worker cleanup incomplete: {report.name}; {report.detail}"
