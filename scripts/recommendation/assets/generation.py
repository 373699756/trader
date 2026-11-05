"""Generate the packaged long-watchlist JavaScript asset from its JSON source."""

from __future__ import annotations

import json

from scripts.core.paths import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "long_watchlist.json"
ASSET_PATH = PROJECT_ROOT / "src" / "trader" / "web" / "static" / "long_watchlist_data.js"
PREFIX = '(function(){"use strict";window.TraderLongWatchlistData=Object.freeze('
SUFFIX = ");})();\n"


def RenderAsset() -> str:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    compact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{PREFIX}{compact_json}{SUFFIX}"


def GenerateAsset() -> None:
    """Explicitly replace the packaged asset with its deterministic rendering."""
    ASSET_PATH.write_text(RenderAsset(), encoding="utf-8")
