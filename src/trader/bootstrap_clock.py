"""Shared bootstrap time helper."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return current UTC time with timezone attached."""

    return datetime.now(timezone.utc)


__all__ = ["utc_now"]
