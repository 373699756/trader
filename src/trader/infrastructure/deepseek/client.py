"""Bounded HTTP client for DeepSeek chat completions."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

import requests


class HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


PostFunction = Callable[..., HttpResponse]


@dataclass(frozen=True)
class DeepSeekHttpAttempt:
    http_status: int | None
    succeeded: bool
    timed_out: bool
    error: str
    latency_ms: float
    token_count: int


@dataclass(frozen=True)
class DeepSeekHttpResult:
    content: str | None
    status_code: int | None
    attempts: int
    timed_out: bool
    error: str
    usage: Mapping[str, object] = field(default_factory=dict)
    attempt_records: tuple[DeepSeekHttpAttempt, ...] = ()


class DeepSeekHttpClient:
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self, post: PostFunction | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._post = post if post is not None else cast(PostFunction, requests.post)
        self._sleep = sleep

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: Sequence[Mapping[str, str]],
        timeout_seconds: float,
        max_tokens: int,
        reserve_attempt: Callable[[], bool],
    ) -> DeepSeekHttpResult:
        if not api_key:
            return DeepSeekHttpResult(None, None, 0, False, "api_key_missing")
        last_error = ""
        last_status: int | None = None
        timed_out = False
        attempts = 0
        attempt_records: list[DeepSeekHttpAttempt] = []
        for attempt in range(2):
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
                response = self._post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": list(messages),
                        "temperature": 0,
                        "max_tokens": max_tokens,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=timeout_seconds,
                )
                attempt_status = response.status_code
                last_status = attempt_status
                if response.status_code in self.RETRYABLE_STATUS_CODES and attempt == 0:
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
                    self._sleep(_retry_delay(response))
                    continue
                response.raise_for_status()
                payload = response.json()
                content, usage = _extract_content(payload)
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
                )
            except requests.Timeout as exc:
                timed_out = True
                last_error = str(exc) or "request_timed_out"
                attempt_timed_out = True
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                last_error = str(exc) or exc.__class__.__name__
                attempt_timed_out = False
            attempt_records.append(
                _attempt_record(
                    attempt_status,
                    succeeded=False,
                    timed_out=attempt_timed_out,
                    error=last_error,
                    started=attempt_started,
                )
            )
            if attempt == 0:
                self._sleep(0.2)
        return DeepSeekHttpResult(
            None,
            last_status,
            attempts,
            timed_out,
            last_error or "request_failed",
            attempt_records=tuple(attempt_records),
        )


def _extract_content(payload: object) -> tuple[str, Mapping[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError("DeepSeek response root must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("DeepSeek response is missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("DeepSeek response is missing message content")
    usage = payload.get("usage")
    return message["content"], usage if isinstance(usage, dict) else {}


def _retry_delay(response: HttpResponse) -> float:
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


__all__ = ["DeepSeekHttpAttempt", "DeepSeekHttpClient", "DeepSeekHttpResult"]
