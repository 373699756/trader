"""Route registration for the read-only desktop product surface."""

from __future__ import annotations

from flask import Flask

from trader.http_api.handlers.product_handler import register_routes
from trader.http_api.route_services import UnifiedWebServices

__all__ = ["register_routes"]


def register_page_routes(app: Flask, services: UnifiedWebServices | None) -> None:
    """Register all page and read-only API routes in one place."""

    register_routes(app, services)
