"""Ports for training commands and due-state evaluation."""

from __future__ import annotations

from typing import Protocol, TypeVar

_ResultT = TypeVar("_ResultT", covariant=True)
_QueryT = TypeVar("_QueryT", contravariant=True)


class TrainingDuePort(Protocol[_QueryT, _ResultT]):
    """Read-only training cadence freeze supplied by infrastructure."""

    def __call__(self, query: _QueryT) -> _ResultT: ...


__all__ = ["TrainingDuePort"]
