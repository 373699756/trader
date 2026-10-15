"""Shared bootstrap time helper."""

from __future__ import annotations

from datetime import datetime, timezone


def UtcNow() -> datetime:
    """Return current UTC time with timezone attached."""

    return datetime.now(timezone.utc)


__all__ = ["UtcNow"]
