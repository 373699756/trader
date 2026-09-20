"""Dashboard page context boundary."""

from __future__ import annotations

from trader.http_api.route_services import UnifiedWebServices


def dashboard_context(services: UnifiedWebServices | None) -> dict[str, int]:
    retention_seconds = services.config.snapshot_retention_seconds if services is not None else 0.0
    return {"web_snapshot_retention_ms": round(retention_seconds * 1000)}


__all__ = ["dashboard_context"]
