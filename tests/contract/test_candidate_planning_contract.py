from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_candidate_planning_contract_is_synchronized_across_authoritative_documents() -> None:
    strategy = _read("docs/01_评分逻辑.md")
    design = _read("docs/02_工程设计.md")
    work = _read("docs/03_工程实施.md")
    changelog = _read("CHANGELOG.md")

    compact_strategy = "".join(strategy.split())
    assert "资格、候选分和稳定排序只有一个领域所有者" in compact_strategy
    assert "某项定向行情缺失、过期、结构非法或刷新后不再满足资格/最低分时" in compact_strategy
    assert "最坏并集为 1080 只" in design
    assert "最终 `ScoredNativeInput.requested_codes` 与候选特征按策略隔离" in design
    assert "candidate_eligibility_before_board_cap" not in work
    assert "candidate-eligibility-before-cap-single-owner" in changelog


def test_runtime_has_no_generic_candidate_weight_owner() -> None:
    runtime = _read("src/trader/application/market_data/input_runtime.py")
    config = _read("config/strategy.json")
    settings = _read("src/trader/infra/settings/models.py")
    performance = _read("src/trader/entrypoints/performance.py")

    assert "_CANDIDATE_WEIGHTS" not in runtime
    assert '"candidate_weights"' not in config
    assert "candidate_weights: Mapping[str, float]" not in settings
    assert "from trader.domain.recommendation.selection.ranking import candidate_score" not in performance
    assert "candidate_score(" not in performance
    assert "_CANDIDATE_WEIGHTS" not in performance
    assert "trader.application.recommendation.candidate_planning import" in performance
    assert "build_candidate_plans" in performance
    assert (
        '"board_preselection": "trader.application.recommendation.candidate_planning.build_candidate_plans"'
        in performance
    )
