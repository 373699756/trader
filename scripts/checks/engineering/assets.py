"""Check the deterministic packaged watchlist without writing it."""

import argparse

from scripts.generate_long_watchlist_asset import ASSET_PATH, render_asset
from scripts.checks.reporting import emit_report


def Main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    expected = render_asset()
    actual = ASSET_PATH.read_text(encoding="utf-8") if ASSET_PATH.is_file() else ""
    passed = expected == actual
    emit_report({
        "schema_version": "asset-consistency-v1",
        "status": "passed" if passed else "failed",
        "summary": {"asset_matches_source": passed, "read_only": True},
    })
    return 0 if passed else 1
