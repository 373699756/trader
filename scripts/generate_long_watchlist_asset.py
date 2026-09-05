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
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the packaged asset is not the deterministic rendering of the JSON source",
    )
    args = parser.parse_args()
    expected = render_asset()
    if args.check:
        actual = ASSET_PATH.read_text(encoding="utf-8") if ASSET_PATH.exists() else ""
        if actual != expected:
            print(f"long watchlist asset is stale; run {Path(__file__).relative_to(PROJECT_ROOT)} without --check")
            return 1
        return 0
    ASSET_PATH.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
