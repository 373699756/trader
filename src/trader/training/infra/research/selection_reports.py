"""Append-only full-population reports; old study files are never rewritten."""

from __future__ import annotations

from pathlib import Path

from trader.infra.serialization.canonical import CanonicalJsonText
from trader.infra.serialization.sealing import PublishImmutable


def PublishSelectionReport(path: Path, payload: dict[str, object]) -> None:
    rendered = CanonicalJsonText(payload)
    if not PublishImmutable(path, rendered) and path.read_text(encoding="utf-8") != rendered:
        raise RuntimeError("selection report conflicts with the immutable published content")


__all__ = ["PublishSelectionReport"]
