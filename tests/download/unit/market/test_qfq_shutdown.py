"""Market cleanup reports every resource without losing committed prices."""

import pytest

from trader.download.infra.qfq_update_runner import QfqShutdown, QfqShutdownScope, StopQfqWorkers
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.infra.shutdown import ShutdownBudget, ShutdownStep


@pytest.mark.parametrize("failure", ("none", "writer_incomplete", "downloads_incomplete", "writer_error", "downloads_error"))
def test_qfq_cleanup_attempts_both_workers_with_one_deadline(failure):
    calls, diagnostics, messages = [], [], []

    class Worker:
        def __init__(self, name):
            self.name = name

        def stop(self, *, wait, cancel_futures, deadline):
            assert wait and cancel_futures
            calls.append((self.name, deadline))
            if failure == self.name + "_error":
                raise RuntimeError("private resource failure")
            return ShutdownStep(self.name, failure != self.name + "_incomplete", False)

    report = StopQfqWorkers(
        QfqShutdown(Worker("writer"), Worker("downloads"), messages.append, lambda context, exc: diagnostics.append((context, exc))),
        0.5,
        0.0,
    )
    assert [name for name, _ in calls] == ["writer", "downloads"]
    assert calls[0][1] is calls[1][1]
    assert len(report.steps) == 2
    assert report.completed is (failure == "none")
    assert len(messages) == len(diagnostics) == int(failure != "none")
    assert "private" not in str(report)


@pytest.mark.parametrize("failure", ("none", "writer", "downloads", "body"))
def test_scope_preserves_original_failure_and_closes_all_attempted_resources(failure):
    events, diagnostics = [], []
    cause = RuntimeError("controlled resource failure")

    class Worker:
        def __init__(self, name):
            self.name = name

        def start(self):
            events.append("start:" + self.name)
            if failure == self.name:
                raise cause
            return True

        def stop(self, *, wait, cancel_futures, deadline):
            events.append("stop:" + self.name)
            return ShutdownStep(self.name, True, False)

    scope = QfqShutdownScope(
        QfqShutdown(Worker("writer"), Worker("downloads"), lambda _message: None, lambda context, exc: diagnostics.append((context, exc))),
        ShutdownBudget(0.5), 0.0, lambda: False,
    )
    result = QfqUpdateResult(None, changed_rows=42)
    with pytest.raises(RuntimeError, match="qfq_shutdown_report_missing"):
        scope.finish(result)
    if failure == "none":
        with scope:
            pass
        assert scope.finish(result) is result
        assert not diagnostics
    else:
        with pytest.raises(RuntimeError) as caught, scope:
            if failure == "body":
                raise cause
            pytest.fail("a failed start entered the operation")
        assert caught.value is cause
        assert len(diagnostics) == 1 and diagnostics[0][1] is cause
    assert events[-2:] == ["stop:writer", "stop:downloads"]
