"""Explicit, separately selected watchlist consistency and generation actions."""

from __future__ import annotations

import argparse

from scripts.core.contracts import ToolOutcome


def Run(action_identity: str, arguments: list[str]) -> ToolOutcome:
    """Run one explicit action; help never reads the source or writes the asset."""
    if action_identity not in {"assets", "assets-generate"}:
        raise ValueError("unknown_asset_action")
    description = "Check the packaged watchlist without writing it." if action_identity == "assets" else "Explicitly regenerate the packaged watchlist JavaScript from its JSON source."
    argparse.ArgumentParser(description=description).parse_args(arguments)
    from scripts.recommendation.assets import generation

    schema = "asset-consistency" if action_identity == "assets" else "asset-generation"
    try:
        if action_identity == "assets-generate":
            generation.GenerateAsset()
            return ToolOutcome(0, {
                "schema_version": schema,
                "status": "passed",
                "state": "generated",
                "summary": {"asset_generated": True, "read_only": False},
            })
        expected = generation.RenderAsset()
        path = generation.ASSET_PATH
        actual = path.read_text(encoding="utf-8") if path.is_file() else ""
        passed = expected == actual
        return ToolOutcome(0 if passed else 1, {
            "schema_version": schema,
            "status": "passed" if passed else "failed",
            "summary": {"asset_matches_source": passed, "read_only": True},
        })
    except (OSError, ValueError):
        return ToolOutcome(1, {
            "schema_version": schema,
            "status": "failed",
            "state": "failed",
            "error_code": "asset_consistency_failed" if action_identity == "assets" else "asset_generation_failed",
        })
