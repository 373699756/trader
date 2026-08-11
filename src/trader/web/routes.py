"""Registration facade for the V2-only read surface."""

from __future__ import annotations

from flask import Flask

from trader.web.route_services import UnifiedWebServices
from trader.web.routes_v2 import create_v2_blueprint


def register_routes(app: Flask, services: UnifiedWebServices | None) -> None:
    app.register_blueprint(create_v2_blueprint(services))


__all__ = [
    "register_routes",
]
