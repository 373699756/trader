"""Static dashboard asset URL helpers."""

from __future__ import annotations

from flask import url_for

from trader.application.decisions.decision_queries import DECISION_VIEW_SCHEMA_VERSION

STATUS_SCHEMA_VERSION = "v2_status_v13"


def web_asset(filename: str) -> str:
    return url_for("static", filename=filename)


__all__ = [
    "DECISION_VIEW_SCHEMA_VERSION",
    "STATUS_SCHEMA_VERSION",
    "web_asset",
]
