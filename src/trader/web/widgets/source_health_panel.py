"""Read-only source health widget boundary."""

from __future__ import annotations

from collections.abc import Mapping


def source_health(status: Mapping[str, object]) -> Mapping[str, object]:
    value = status.get("market_data", {})
    return value if isinstance(value, Mapping) else {}


__all__ = ["source_health"]
