"""Side-effect-free Flask application factory."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from flask import Flask, Response, abort, request

from trader.web.api.route_services import UnifiedWebServices
from trader.web.api.routes import register_routes
from trader.web.static_assets import web_asset


@dataclass(frozen=True)
class _StaticAsset:
    content: bytes
    content_type: str
    etag: str


def create_app(
    *,
    services: UnifiedWebServices | None = None,
) -> Flask:
    assets = _snapshot_static_assets()
    app = Flask(__name__, template_folder="templates", static_folder=None)
    app.add_url_rule(
        "/static/<path:filename>",
        endpoint="static",
        view_func=lambda filename: _static_response(assets, filename),
    )
    app.add_template_global(web_asset, "web_asset")
    app.jinja_env.get_template("index.html")
    register_routes(app, services)
    return app


def _snapshot_static_assets() -> dict[str, _StaticAsset]:
    root = Path(__file__).with_name("static")
    assets: dict[str, _StaticAsset] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        assets[path.name] = _StaticAsset(content, content_type, hashlib.sha256(content).hexdigest())
    return assets


def _static_response(assets: dict[str, _StaticAsset], filename: str) -> Response:
    asset = assets.get(filename)
    if asset is None:
        abort(404)
    if request.if_none_match.contains(asset.etag):
        response = Response(status=304)
    else:
        response = Response(asset.content, content_type=asset.content_type)
    response.set_etag(asset.etag)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


__all__ = ["create_app"]
