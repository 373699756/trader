"""Side-effect-free Flask application factory."""

from __future__ import annotations

from flask import Flask

from trader.web.route_services import UnifiedWebServices
from trader.web.routes import register_routes
from trader.web.static_assets import web_asset


def create_app(
    *,
    services: UnifiedWebServices | None = None,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.add_template_global(web_asset, "web_asset")
    register_routes(app, services)
    return app


__all__ = ["create_app"]
