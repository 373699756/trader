"""Bounded HTTP client for DeepSeek chat completions."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    from typing_extensions import Unpack

import requests

from trader.infra.deepseek.base_client import (
    CompletionOptions,
    DeepSeekClientBase,
    DeepSeekHttpAttempt,
    DeepSeekHttpResult,
    ModelCapabilities,
)
from trader.infra.deepseek.model_capabilities import capabilities as _lookup_capabilities
from trader.infra.deepseek.model_catalog import validate_model
from trader.infra.failures import classify_adapter_failure

_POST_TYPE = Callable[..., "requests.Response"]


@dataclass(frozen=True)
class _HttpAttemptRequest:
    base_url: str
    api_key: str
    model: str
    messages: Sequence[Mapping[str, Any]]
    timeout_seconds: float
    max_tokens: int
    capabilities: ModelCapabilities


@dataclass(frozen=True)
class _AttemptOutcome:
    result: DeepSeekHttpResult | None
    status: int | None
    error: str
    timed_out: bool
    should_retry: bool
    retry_delay: float
    record: DeepSeekHttpAttempt


class _AttemptRecordRequiredOptions(TypedDict):
    succeeded: bool
    timed_out: bool
    error: str
    started: float


class _AttemptRecordOptionalOptions(TypedDict, total=False):
    token_count: int


class _AttemptRecordOptions(_AttemptRecordRequiredOptions, _AttemptRecordOptionalOptions):
    pass


class _FailedAttemptOptions(TypedDict):
    timed_out: bool
    should_retry: bool
    retry_delay: float
    started: float


class DeepSeekHttpClient(DeepSeekClientBase):
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, post: _POST_TYPE | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._post = post if post is not None else cast(_POST_TYPE, requests.post)
        self._sleep = sleep

    def capabilities(self, model: str) -> ModelCapabilities:
        return _lookup_capabilities(model)

    def complete(
        self,
        **options: Unpack[CompletionOptions],
    ) -> DeepSeekHttpResult:
        base_url = options["base_url"]
        api_key = options["api_key"]
        model = options["model"]
        messages = options["messages"]
        timeout_seconds = options["timeout_seconds"]
        max_tokens = options["max_tokens"]
        reserve_attempt = options["reserve_attempt"]
        maximum_attempts = options.get("maximum_attempts", 2)
        if not api_key:
            return DeepSeekHttpResult(None, None, 0, False, "api_key_missing")
        if not 1 <= maximum_attempts <= 2:
            raise ValueError("DeepSeek batch maximum attempts must be between 1 and 2")
        validate_model(model)
        caps = _lookup_capabilities(model)
        request = _HttpAttemptRequest(base_url, api_key, model, messages, timeout_seconds, max_tokens, caps)
        last_error = ""
        last_status: int | None = None
        timed_out = False
        attempts = 0
        attempt_records: list[DeepSeekHttpAttempt] = []
        for attempt in range(maximum_attempts):
            if not reserve_attempt():
                return DeepSeekHttpResult(
                    None,
                    last_status,
                    attempts,
                    timed_out,
                    "budget_exhausted",
                    attempt_records=tuple(attempt_records),
                )
            attempts += 1
            outcome = self._perform_attempt(request)
            last_status = outcome.status
            last_error = outcome.error
            timed_out = timed_out or outcome.timed_out
            attempt_records.append(outcome.record)
            if outcome.result is not None:
                return replace(outcome.result, attempts=attempts, attempt_records=tuple(attempt_records))
            if outcome.should_retry and attempt + 1 < maximum_attempts:
                self._sleep(outcome.retry_delay)
                continue
            break
        return DeepSeekHttpResult(
            None,
            last_status,
            attempts,
            timed_out,
            last_error or "request_failed",
            attempt_records=tuple(attempt_records),
            failure=classify_adapter_failure(
                RuntimeError("timeout" if timed_out else (last_error or "request_failed")),
                provider="deepseek",
                operation="chat_completion",
            ),
        )

    def _perform_attempt(self, request: _HttpAttemptRequest) -> _AttemptOutcome:
        attempt_started = time.perf_counter()
        status: int | None = None
        try:
            response = self._post(
                f"{request.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {request.api_key}", "Content-Type": "application/json"},
                json=_request_payload(request.model, request.messages, request.max_tokens, request.capabilities),
                timeout=request.timeout_seconds,
            )
            status = response.status_code
            if status in self.RETRYABLE_STATUS_CODES:
                error = f"http_{status}"
                return _failed_attempt(
                    status,
                    error,
                    timed_out=False,
                    should_retry=True,
                    retry_delay=_retry_delay(response),
                    started=attempt_started,
                )
            response.raise_for_status()
            content, usage, actual_model, fingerprint, finish_reason, reasoning = _extract_content(response.json())
            record = _attempt_record(
                status,
                succeeded=True,
                timed_out=False,
                error="",
                started=attempt_started,
                token_count=_token_count(usage),
            )
            result = DeepSeekHttpResult(
                content,
                status,
                0,
                False,
                "",
                usage,
                (),
                actual_model,
                fingerprint,
                finish_reason,
                _usage_integer(usage, "prompt_cache_hit_tokens"),
                _usage_integer(usage, "prompt_cache_miss_tokens"),
                reasoning,
            )
            return _AttemptOutcome(result, status, "", False, False, 0.0, record)
        except requests.Timeout as exc:
            return _failed_attempt(
                status,
                str(exc) or "request_timed_out",
                timed_out=True,
                should_retry=True,
                retry_delay=0.2,
                started=attempt_started,
            )
        except requests.HTTPError as exc:
            return _failed_attempt(
                status,
                str(exc) or exc.__class__.__name__,
                timed_out=False,
                should_retry=False,
                retry_delay=0.0,
                started=attempt_started,
            )
        except requests.RequestException as exc:
            return _failed_attempt(
                status,
                str(exc) or exc.__class__.__name__,
                timed_out=False,
                should_retry=True,
                retry_delay=0.2,
                started=attempt_started,
            )
        except (ValueError, TypeError, KeyError) as exc:
            return _failed_attempt(
                status,
                str(exc) or exc.__class__.__name__,
                timed_out=False,
                should_retry=True,
                retry_delay=0.2,
                started=attempt_started,
            )


def _request_payload(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int,
    caps: ModelCapabilities,
) -> dict[str, object]:
    normalized_messages: list[dict[str, Any]] = []
    for msg in messages:
        entry: dict[str, Any] = {"role": msg["role"]}
        content = msg.get("content")
        if content is not None:
            entry["content"] = content
        if caps.requires_reasoning_roundtrip:
            reasoning = msg.get("reasoning_content")
            if reasoning is not None:
                entry["reasoning_content"] = reasoning
        normalized_messages.append(entry)

    payload: dict[str, object] = {
        "model": model,
        "messages": normalized_messages,
        "max_tokens": max_tokens,
    }

    if caps.preferred_structured_method == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif caps.preferred_structured_method == "function_calling":
        pass

    if not caps.requires_reasoning_roundtrip:
        payload["temperature"] = 0

    if caps.reasoning_effort is not None:
        payload["reasoning_effort"] = caps.reasoning_effort

    return payload


def _extract_content(
    payload: object,
) -> tuple[str, Mapping[str, object], str | None, str | None, str | None, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek response root must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("DeepSeek response is missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("DeepSeek response is missing message content")
    usage = payload.get("usage")
    actual_model = payload.get("model")
    system_fingerprint = payload.get("system_fingerprint")
    finish_reason = choices[0].get("finish_reason")
    reasoning_content = message.get("reasoning_content")
    return (
        message["content"],
        usage if isinstance(usage, dict) else {},
        actual_model if isinstance(actual_model, str) and actual_model else None,
        system_fingerprint if isinstance(system_fingerprint, str) and system_fingerprint else None,
        finish_reason if isinstance(finish_reason, str) and finish_reason else None,
        reasoning_content if isinstance(reasoning_content, str) and reasoning_content else None,
    )


def _retry_delay(response: requests.Response) -> float:
    raw = response.headers.get("Retry-After", "")
    try:
        return min(5.0, max(0.0, float(raw)))
    except ValueError:
        return 0.2


def _attempt_record(
    http_status: int | None,
    **options: Unpack[_AttemptRecordOptions],
) -> DeepSeekHttpAttempt:
    succeeded = options["succeeded"]
    timed_out = options["timed_out"]
    error = options["error"]
    return DeepSeekHttpAttempt(
        http_status=http_status,
        succeeded=succeeded,
        timed_out=timed_out,
        error=error,
        latency_ms=(time.perf_counter() - options["started"]) * 1000.0,
        token_count=options.get("token_count", 0),
        failure=(
            None
            if succeeded
            else classify_adapter_failure(
                RuntimeError("timeout" if timed_out else (error or "request_failed")),
                provider="deepseek",
                operation="chat_completion",
            )
        ),
    )


def _failed_attempt(
    status: int | None,
    error: str,
    **options: Unpack[_FailedAttemptOptions],
) -> _AttemptOutcome:
    timed_out = options["timed_out"]
    return _AttemptOutcome(
        None,
        status,
        error,
        timed_out,
        options["should_retry"],
        options["retry_delay"],
        _attempt_record(
            status,
            succeeded=False,
            timed_out=timed_out,
            error=error,
            started=options["started"],
        ),
    )


def _token_count(usage: Mapping[str, object]) -> int:
    raw = usage.get("total_tokens")
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


def _usage_integer(usage: Mapping[str, object], key: str) -> int:
    raw = usage.get(key)
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


__all__ = ["DeepSeekHttpAttempt", "DeepSeekHttpClient", "DeepSeekHttpResult"]
