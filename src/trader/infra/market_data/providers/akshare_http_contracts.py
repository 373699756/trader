"""Shared HTTP transport contract for AKShare-compatible providers."""

from collections.abc import Callable
from typing import Protocol


class AkshareHttpResponse(Protocol):
    text: str

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


AkshareGetFunction = Callable[..., AkshareHttpResponse]

__all__ = ["AkshareGetFunction", "AkshareHttpResponse"]
