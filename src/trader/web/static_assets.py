"""Static dashboard asset URL helpers."""

from __future__ import annotations

from flask import url_for


def WebAsset(filename: str) -> str:
    return url_for("static", filename=filename)


__all__ = ["WebAsset"]
