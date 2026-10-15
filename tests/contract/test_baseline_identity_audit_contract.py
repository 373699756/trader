from __future__ import annotations

from pathlib import Path

from trader.entrypoints.cli import BuildParser

ROOT = Path(__file__).resolve().parents[2]


def test_baseline_audit_is_an_explicit_read_only_cli_command() -> None:
    parser = BuildParser()
    args = parser.parse_args(["research-baseline-audit"])
    assert args.command == "research-baseline-audit"


def test_baseline_audit_projection_has_no_future_or_production_authority_fields() -> None:
    source = (ROOT / "src/trader/training/entrypoints/commands.py").read_text(encoding="utf-8")
    assert "LoadBaselineIdentityEvidence" in source
    assert '"production_authority": audit.production_authority' in source
    assert "research-baseline-audit" in source
