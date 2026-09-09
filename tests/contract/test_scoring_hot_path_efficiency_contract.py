from __future__ import annotations

from pathlib import Path

from trader.application.research.scoring_hot_path_baseline import ScoringHotPathBaseline
from trader.entrypoints.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]


def test_scoring_hot_path_baseline_has_explicit_cli_and_fixed_identity() -> None:
    args = build_parser().parse_args(["research-scoring-hot-path-baseline"])
    assert args.command == "research-scoring-hot-path-baseline"
    assert ScoringHotPathBaseline.__dataclass_fields__["schema_version"].default == (
        "scoring_hot_path_efficiency_baseline"
    )


def test_strategy_contract_requires_all_hot_path_denominators_and_equivalence_cases() -> None:
    source = (ROOT / "src/trader/application/research/scoring_hot_path_baseline.py").read_text(encoding="utf-8")

    for token in (
        "completed_epoch_count",
        "evaluated_candidate_count",
        "formal_current_decision_count",
        "formal_frozen_decision_count",
        "deepseek_candidate_count",
        "ScoringHotPathEquivalence",
        "result_hash",
        "allocation_growth_percent",
        "allocation_budget_passed",
    ):
        assert token in source
