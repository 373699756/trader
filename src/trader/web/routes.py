"""Registration facade for read-only Web route groups."""

from __future__ import annotations

from flask import Flask

from trader.web.route_services import StatusProvider, WebApiConfig, WebServices
from trader.web.routes_events import create_event_blueprint
from trader.web.routes_recommendations import create_recommendation_blueprint
from trader.web.routes_status import create_status_blueprint


def register_routes(app: Flask, services: WebServices) -> None:
    app.register_blueprint(create_status_blueprint(services))
    app.register_blueprint(create_recommendation_blueprint(services))
    app.register_blueprint(create_event_blueprint(services))


__all__ = ["StatusProvider", "WebApiConfig", "WebServices", "register_routes"]
