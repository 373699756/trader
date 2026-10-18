"""Project-owned HTTP values; no third-party client types cross this boundary."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlencode

HttpParameter = str | int | float | bool | None | Sequence[str]


class HttpError(OSError):
    """A bounded transport failure with no URL, credentials or remote payload."""


class HttpTimeout(HttpError, TimeoutError):
    pass


class HttpConnectionError(HttpError):
    pass


class HttpCancelled(HttpError):
    pass


class HttpResponseTooLarge(HttpError):
    pass


class HttpStatusError(HttpError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"http_{status_code}")


class _Headers(Mapping[str, str]):
    def __init__(self, items: tuple[tuple[str, str], ...]) -> None:
        self._items = {key.lower(): value for key, value in items}

    def __getitem__(self, key: str) -> str:
        return self._items[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True)
class HttpRequest:
    method: Literal["GET", "POST", "DELETE"]
    url: str
    timeout_seconds: float
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    deadline: float | None = None
    max_response_bytes: int = 16 * 1024 * 1024
    cancel_requested: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("http_timeout_invalid")
        if self.deadline is not None and not math.isfinite(self.deadline):
            raise ValueError("http_deadline_invalid")
        if self.max_response_bytes <= 0:
            raise ValueError("http_response_limit_invalid")


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes
    header_items: tuple[tuple[str, str], ...] = ()
    encoding: str = "utf-8"

    @property
    def headers(self) -> Mapping[str, str]:
        return _Headers(self.header_items)

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding, errors="replace")
        except LookupError as exc:
            raise HttpError("http_encoding_unsupported") from exc

    def json(self) -> object:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        # No implicit redirects: one request is exactly one physical attempt.
        if not 200 <= self.status_code < 300:
            raise HttpStatusError(self.status_code)


@dataclass(frozen=True)
class HttpAttempt:
    host: str
    method: str
    elapsed_seconds: float
    response_bytes: int
    status_code: int | None
    failure: str


class HttpTransport(Protocol):
    def execute(self, request: HttpRequest) -> HttpResponse: ...

    def close(self) -> None: ...

    def __enter__(self) -> HttpTransport: ...

    def __exit__(self, *args: object) -> None: ...

    def get(
        self,
        url: str,
        *,
        timeout: float,
        params: Mapping[str, HttpParameter] | None = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HttpResponse: ...

    def post(
        self,
        url: str,
        *,
        timeout: float,
        params: Mapping[str, HttpParameter] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        json: object = None,
        deadline: float | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HttpResponse: ...


def EncodeHttpParameters(url: str, parameters: Mapping[str, HttpParameter] | None) -> str:
    if not parameters:
        return url
    for value in parameters.values():
        if (
            value is not None
            and not isinstance(value, (str, int, float, bool))
            and not (isinstance(value, Sequence) and all(isinstance(item, str) for item in value))
        ):
            raise TypeError("http_parameter_invalid")
    query = urlencode(tuple((key, value) for key, value in parameters.items() if value is not None), doseq=True)
    return url + ("&" if "?" in url else "?") + query


__all__ = [
    "EncodeHttpParameters",
    "HttpAttempt",
    "HttpCancelled",
    "HttpConnectionError",
    "HttpError",
    "HttpParameter",
    "HttpRequest",
    "HttpResponse",
    "HttpResponseTooLarge",
    "HttpStatusError",
    "HttpTimeout",
    "HttpTransport",
]
