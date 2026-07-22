"""Abstract base for DeepSeek chat completion clients."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelCapabilities:
    """Declarative capabilities table for a DeepSeek model.

    Attributes:
        preferred_structured_method: One of ``"json_object"``, ``"function_calling"``,
            or ``"none"``.  Controls how structured output is requested.
        requires_reasoning_roundtrip: When ``True``, messages returned by the model
            may contain a ``reasoning_content`` field that must be forwarded on
            the next turn.  ``temperature`` / ``top_p`` must not be sent.
        supports_tool_choice: Whether the model accepts ``tool_choice``.
        reasoning_effort: When not ``None``, this is sent as the
            ``reasoning_effort`` parameter (e.g. ``"high"``).
    """

    preferred_structured_method: str  # "json_object" | "function_calling" | "none"
    requires_reasoning_roundtrip: bool = False
    supports_tool_choice: bool = False
    reasoning_effort: str | None = None


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
    actual_model: str | None = None
    system_fingerprint: str | None = None
    finish_reason: str | None = None
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    reasoning_content: str | None = field(default=None, repr=False)


class DeepSeekClientBase(ABC):
    """Interface that every DeepSeek HTTP transport must implement."""

    @abstractmethod
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
    ) -> DeepSeekHttpResult: ...

    @abstractmethod
    def capabilities(self, model: str) -> ModelCapabilities:
        """Return the declared capabilities for *model*."""
        ...


__all__ = [
    "DeepSeekClientBase",
    "DeepSeekHttpAttempt",
    "DeepSeekHttpResult",
    "ModelCapabilities",
]
