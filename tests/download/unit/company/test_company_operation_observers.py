"""A terminal observer must not replace an operation's original failure."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from trader.download.application.company_operations import CompanyOperations


@pytest.mark.parametrize("execution", ("call", "inline_future", "worker_future"))
@pytest.mark.parametrize("cancelled", (False, True))
def test_terminal_observer_failure_preserves_primary_exception_and_cause(caplog, execution, cancelled):
    primary = InterruptedError("company_sync_cancelled") if cancelled else RuntimeError("fixture_primary_failure")
    cause = OSError("private source detail")
    recorded = []

    def observe(event):
        if event.operation is not None and event.operation.state in {"failed", "cancelled"}:
            raise ValueError("private observer detail")

    operations = CompanyOperations(observe, lambda: 0, (), failures=lambda context, error: recorded.append(error))

    def fail():
        raise primary from cause

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(type(primary)) as raised:
            if execution == "call":
                operations.call("text", fail)
            else:
                future = operations.submit("text", fail, executor if execution == "worker_future" else None)
                try:
                    future.result(timeout=5)
                finally:
                    operations.take_future(future)
    assert raised.value is primary and primary.__cause__ is cause
    if execution == "call":
        assert recorded == ([] if cancelled else [primary])
    assert "terminal observation failed" in caplog.text
    assert "ValueError" in caplog.text
    assert "private observer detail" not in caplog.text and "private source detail" not in caplog.text


@pytest.mark.parametrize("execution", ("call", "inline_future", "worker_future"))
def test_successful_operation_still_propagates_terminal_observer_failure(execution):
    observation_error = RuntimeError("fixture_completed_observer_failed")
    results = []

    def observe(event):
        if event.operation is not None and event.operation.state == "completed":
            raise observation_error

    operations = CompanyOperations(observe, lambda: 0, ())

    def succeed():
        results.append("completed")
        return 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(RuntimeError) as raised:
            if execution == "call":
                operations.call("text", succeed)
            else:
                future = operations.submit("text", succeed, executor if execution == "worker_future" else None)
                try:
                    future.result(timeout=5)
                finally:
                    operations.take_future(future)
    assert raised.value is observation_error and results == ["completed"]
