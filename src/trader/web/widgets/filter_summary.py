"""Read-only filter summary widget boundary."""

from __future__ import annotations

from collections.abc import Mapping


def filter_summary(status: Mapping[str, object]) -> Mapping[str, object]:
    summary = status.get("filter_summary")
    return summary if isinstance(summary, Mapping) else {}


__all__ = ["filter_summary"]
