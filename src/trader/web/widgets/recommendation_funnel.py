"""Read-only recommendation funnel widget boundary."""

from __future__ import annotations

from collections.abc import Mapping


def funnel_status(status: Mapping[str, object]) -> Mapping[str, object]:
    """Return the already-projected funnel status without querying business state."""

    return status.get("funnel", {}) if isinstance(status.get("funnel"), Mapping) else {}


__all__ = ["funnel_status"]
