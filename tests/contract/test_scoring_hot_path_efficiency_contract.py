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
    work = " ".join((ROOT / "docs/work.md").read_text(encoding="utf-8").split())
    for token in (
        "每个完成评分 epoch",
        "每个被评估候选",
        "每次正式 current/frozen 决策",
        "每个实际 DeepSeek 候选",
        "相同输入的候选、分数、风险、动作、排名和决策 hash 完全一致",
        "100 tick 分配增长不超过 20%",
    ):
        assert token in work
