"""Shape predicates shared by published JSON decoders.

Decoded JSON values must be narrowed before they may enter business state, and these
rules are easy to get subtly wrong: ``bool`` is a subclass of ``int``, JSON numbers
must be finite to be usable, and content hashes are lowercase hexadecimal text. Codec
modules keep their own error types and messages and share the narrowing rules here.

Every predicate is a :class:`TypeGuard` so ``if not is_boolean(value): raise ...``
narrows the caller's ``object`` to the decoded type instead of forcing a ``cast``.
"""

from __future__ import annotations

import math
from typing import TypeGuard, cast

_HEX_DIGITS = frozenset("0123456789abcdef")


def IsNonEmptyText(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is a non-empty string."""

    return isinstance(value, str) and bool(value)


def IsInteger(value: object) -> TypeGuard[int]:
    """Return whether ``value`` is an integer and not a boolean."""

    return isinstance(value, int) and not isinstance(value, bool)


def IsNumber(value: object) -> TypeGuard[float]:
    """Return whether ``value`` is a number and not a boolean."""

    return isinstance(value, (int, float)) and not isinstance(value, bool)


def IsFiniteNumber(value: object) -> TypeGuard[float]:
    """Return whether ``value`` is a finite number and not a boolean."""

    if not IsNumber(value):
        return False
    return math.isfinite(value)


def IsBoolean(value: object) -> TypeGuard[bool]:
    """Return whether ``value`` is a boolean."""

    return isinstance(value, bool)


def IsSha256Text(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is lowercase hexadecimal SHA-256 text."""

    return isinstance(value, str) and len(value) == 64 and all(character in _HEX_DIGITS for character in value)


def IsTextSequence(value: object) -> TypeGuard[list[str]]:
    """Return whether ``value`` is a sequence whose items are all strings."""

    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def AsSequence(value: object) -> list[object] | None:
    """Return ``value`` as a list when it is one, otherwise ``None``."""

    if not isinstance(value, list):
        return None
    return cast(list[object], value)


__all__ = [
    "AsSequence",
    "IsBoolean",
    "IsFiniteNumber",
    "IsInteger",
    "IsNonEmptyText",
    "IsNumber",
    "IsSha256Text",
    "IsTextSequence",
]
