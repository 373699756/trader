from __future__ import annotations

from concurrent.futures import CancelledError

from trader.infra.failures import AdapterFailureCode, ClassifyAdapterFailure


def test_adapter_failure_classification_is_shared_and_structured() -> None:
    timeout = ClassifyAdapterFailure(
        TimeoutError("transport timed out"),
        provider="deepseek",
        operation="chat_completion",
    )
    cancelled = ClassifyAdapterFailure(
        CancelledError(),
        provider="tencent",
        operation="candidate_quotes",
    )
    circuit = ClassifyAdapterFailure(
        RuntimeError("circuit_open"),
        provider="eastmoney",
        operation="full_market_quotes",
    )
    late = ClassifyAdapterFailure(
        RuntimeError("eastmoney: late"),
        provider="eastmoney",
        operation="full_market_quotes",
    )

    assert timeout.code is AdapterFailureCode.TIMEOUT
    assert timeout.retryable is True
    assert (timeout.provider, timeout.operation) == ("deepseek", "chat_completion")
    assert cancelled.code is AdapterFailureCode.CANCELLED
    assert cancelled.retryable is False
    assert circuit.code is AdapterFailureCode.CIRCUIT_OPEN
    assert circuit.retryable is True
    assert late.code is AdapterFailureCode.DEADLINE


def test_adapter_failure_detail_is_bounded_and_does_not_preserve_secrets() -> None:
    failure = ClassifyAdapterFailure(
        RuntimeError("Authorization: Bearer secret-token"),
        provider="deepseek",
        operation="chat_completion",
    )

    assert failure.code is AdapterFailureCode.SOURCE_FAILED
    assert "secret-token" not in failure.detail
    assert len(failure.detail) <= 240


def test_adapter_failure_keeps_worker_rejection_distinct() -> None:
    failure = ClassifyAdapterFailure(
        RuntimeError("resource_rejected: injected worker queue is full or stopped"),
        provider="worker",
        operation="submit",
    )

    assert failure.code is AdapterFailureCode.RESOURCE_REJECTED
    assert failure.retryable is True
    assert failure.detail == "resource_rejected"
