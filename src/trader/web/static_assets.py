"""Static dashboard asset URL helpers."""

from __future__ import annotations

from flask import url_for


def web_asset(filename: str) -> str:
    return url_for("static", filename=filename)


__all__ = ["web_asset"]
