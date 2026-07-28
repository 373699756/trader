"""Side-effect-free Flask application factory."""

from __future__ import annotations

from flask import Flask

from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries
from trader.web.routes import (
    StatusProvider,
    TomorrowWebServices,
    WebApiConfig,
    WebServices,
    register_routes,
)
from trader.web.static_assets import web_asset


def create_app(
    status_provider: StatusProvider | None = None,
    *,
    queries: RecommendationQueries | None = None,
    publisher: SnapshotPublisher | None = None,
    tomorrow: TomorrowWebServices | None = None,
    api_config: WebApiConfig | None = None,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.add_template_global(web_asset, "web_asset")
    register_routes(
        app,
        WebServices(
            status_provider=status_provider or _not_ready_status,
            queries=queries,
            publisher=publisher,
            tomorrow_queries=tomorrow.queries if tomorrow is not None else None,
            tomorrow_events=tomorrow.events if tomorrow is not None else None,
            config=api_config or WebApiConfig(),
        ),
    )
    return app


def _not_ready_status() -> dict[str, object]:
    return {
        "schema_version": "v2",
        "status": "not_ready",
        "runtime_started": False,
        "degraded_reasons": ["runtime_not_started"],
    }


__all__ = ["create_app"]
