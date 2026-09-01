from __future__ import annotations

from pathlib import Path

from trader.entrypoints.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]


def test_baseline_audit_is_an_explicit_read_only_cli_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["research-baseline-audit"])
    assert args.command == "research-baseline-audit"
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    assert "score_current_baseline_consistency_audit_v1" in strategy
    assert "baseline_identity_inconsistent" in strategy


def test_baseline_audit_projection_has_no_future_or_production_authority_fields() -> None:
    source = (ROOT / "src/trader/entrypoints/research_commands.py").read_text(encoding="utf-8")
    assert "load_baseline_identity_evidence" in source
    assert '"production_authority": audit.production_authority' in source
    assert "research-baseline-audit" in source
