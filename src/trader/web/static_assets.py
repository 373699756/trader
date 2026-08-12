"""Static dashboard asset URL helpers."""

from __future__ import annotations

from flask import url_for

WEB_ASSET_REVISION = "long-research-view-2026-08-12"


def web_asset(filename: str) -> str:
    return url_for("static", filename=filename, rev=WEB_ASSET_REVISION)


__all__ = ["WEB_ASSET_REVISION", "web_asset"]
