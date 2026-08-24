"""Static dashboard asset URL helpers."""

from __future__ import annotations

from flask import url_for

from trader.application.decision_queries import DECISION_VIEW_SCHEMA_VERSION

STATUS_SCHEMA_VERSION = "v2_status_v2"
WEB_ASSET_REVISION = "release-contract-2026-08-24-v12"


def web_asset(filename: str) -> str:
    return url_for("static", filename=filename, rev=WEB_ASSET_REVISION)


__all__ = [
    "DECISION_VIEW_SCHEMA_VERSION",
    "STATUS_SCHEMA_VERSION",
    "WEB_ASSET_REVISION",
    "web_asset",
]
