"""Generate the packaged long-watchlist JavaScript asset from its JSON source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "long_watchlist.json"
ASSET_PATH = PROJECT_ROOT / "src" / "trader" / "web" / "static" / "long_watchlist_data.js"
PREFIX = '(function(){"use strict";window.TraderLongWatchlistData=Object.freeze('
SUFFIX = ");})();\n"


def render_asset() -> str:
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    compact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{PREFIX}{compact_json}{SUFFIX}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    expected = render_asset()
    ASSET_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
