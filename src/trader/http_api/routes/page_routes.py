"""Route registration for the read-only desktop product surface."""

from __future__ import annotations

from flask import Flask

from trader.http_api.handlers.product_handler import RegisterRoutes
from trader.http_api.route_services import WebServices

__all__ = ["RegisterRoutes"]


def RegisterPageRoutes(app: Flask, services: WebServices | None) -> None:
    """Register all page and read-only API routes in one place."""

    RegisterRoutes(app, services)
