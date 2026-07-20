"""Bounded HTTP client for DeepSeek chat completions."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import requests

from trader.infrastructure.deepseek.base_client import (
    DeepSeekClientBase,
    DeepSeekHttpAttempt,
    DeepSeekHttpResult,
    ModelCapabilities,
)
from trader.infrastructure.deepseek.model_capabilities import capabilities as _lookup_capabilities
from trader.infrastructure.deepseek.model_catalog import validate_model

_POST_TYPE = Callable[..., "requests.Response"]


class DeepSeekHttpClient(DeepSeekClientBase):
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, post: _POST_TYPE | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._post = post if post is not None else cast(_POST_TYPE, requests.post)
        self._sleep = sleep

    def capabilities(self, model: str) -> ModelCapabilities:
        return _lookup_capabilities(model)

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        timeout_seconds: float,
        max_tokens: int,
        reserve_attempt: Callable[[], bool],
        maximum_attempts: int = 2,
    ) -> DeepSeekHttpResult:
        if not api_key:
            return DeepSeekHttpResult(None, None, 0, False, "api_key_missing")
        if not 1 <= maximum_attempts <= 2:
            raise ValueError("DeepSeek batch maximum attempts must be between 1 and 2")
        validate_model(model)
        caps = _lookup_capabilities(model)
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
            attempt_status: int | None = None
            attempt_started = time.perf_counter()
            try:
                payload = _request_payload(model, messages, max_tokens, caps)
                response = self._post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout_seconds,
                )
                attempt_status = response.status_code
                last_status = attempt_status
                if response.status_code in self.RETRYABLE_STATUS_CODES:
                    last_error = f"http_{response.status_code}"
                    attempt_records.append(
                        _attempt_record(
                            attempt_status,
                            succeeded=False,
                            timed_out=False,
                            error=last_error,
                            started=attempt_started,
                        )
                    )
                    if attempt + 1 < maximum_attempts:
                        self._sleep(_retry_delay(response))
                        continue
                    break
                response.raise_for_status()
                payload_obj = response.json()
                content, usage, actual_model, system_fingerprint, finish_reason, reasoning_content = _extract_content(
                    payload_obj
                )
                attempt_records.append(
                    _attempt_record(
                        attempt_status,
                        succeeded=True,
                        timed_out=False,
                        error="",
                        started=attempt_started,
                        token_count=_token_count(usage),
                    )
                )
                return DeepSeekHttpResult(
                    content,
                    response.status_code,
                    attempts,
                    False,
                    "",
                    usage,
                    tuple(attempt_records),
                    actual_model,
                    system_fingerprint,
                    finish_reason,
                    _usage_integer(usage, "prompt_cache_hit_tokens"),
                    _usage_integer(usage, "prompt_cache_miss_tokens"),
                    reasoning_content,
                )
            except requests.Timeout as exc:
                timed_out = True
                last_error = str(exc) or "request_timed_out"
                attempt_timed_out = True
                should_retry = True
            except requests.HTTPError as exc:
                last_error = str(exc) or exc.__class__.__name__
                attempt_timed_out = False
                should_retry = False
            except requests.RequestException as exc:
                last_error = str(exc) or exc.__class__.__name__
                attempt_timed_out = False
                should_retry = True
            except (ValueError, TypeError, KeyError) as exc:
                last_error = str(exc) or exc.__class__.__name__
                attempt_timed_out = False
                should_retry = True
            attempt_records.append(
                _attempt_record(
                    attempt_status,
                    succeeded=False,
                    timed_out=attempt_timed_out,
                    error=last_error,
                    started=attempt_started,
                )
            )
            if should_retry and attempt + 1 < maximum_attempts:
                self._sleep(0.2)
                continue
            break
        return DeepSeekHttpResult(
            None,
            last_status,
            attempts,
            timed_out,
            last_error or "request_failed",
            attempt_records=tuple(attempt_records),
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
    *,
    succeeded: bool,
    timed_out: bool,
    error: str,
    started: float,
    token_count: int = 0,
) -> DeepSeekHttpAttempt:
    return DeepSeekHttpAttempt(
        http_status=http_status,
        succeeded=succeeded,
        timed_out=timed_out,
        error=error,
        latency_ms=(time.perf_counter() - started) * 1000.0,
        token_count=token_count,
    )


def _token_count(usage: Mapping[str, object]) -> int:
    raw = usage.get("total_tokens")
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


def _usage_integer(usage: Mapping[str, object], key: str) -> int:
    raw = usage.get(key)
    return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0


__all__ = ["DeepSeekHttpAttempt", "DeepSeekHttpClient", "DeepSeekHttpResult"]
