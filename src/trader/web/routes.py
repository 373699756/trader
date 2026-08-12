"""Registration facade for the current and compatibility read surfaces."""

from __future__ import annotations

from flask import Flask

from trader.web.route_services import UnifiedWebServices
from trader.web.routes_events import create_event_blueprint
from trader.web.routes_recommendations import create_recommendation_blueprint
from trader.web.routes_status import create_status_blueprint
from trader.web.routes_v2 import create_v2_blueprint


def register_routes(app: Flask, services: UnifiedWebServices | None) -> None:
    app.register_blueprint(create_v2_blueprint(services))
    if services is not None and services.legacy is not None:
        app.register_blueprint(create_status_blueprint(services.legacy))
        app.register_blueprint(create_recommendation_blueprint(services.legacy))
        app.register_blueprint(create_event_blueprint(services.legacy))


__all__ = [
    "register_routes",
]
