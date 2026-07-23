"""Dashboard and runtime-status read routes."""

from __future__ import annotations

from typing import cast

from flask import Blueprint, Response, jsonify, render_template

from trader.application.ports.types import JsonValue, thaw_json_value
from trader.web.route_services import WebServices


def create_status_blueprint(services: WebServices) -> Blueprint:
    blueprint = Blueprint("dashboard_status", __name__)

    @blueprint.get("/")
    def dashboard() -> str:
        return render_template("index.html")

    @blueprint.get("/api/status")
    def status() -> Response:
        return jsonify(thaw_json_value(cast(JsonValue, services.status_provider())))

    return blueprint


__all__ = ["create_status_blueprint"]
