from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_candidate_planning_contract_is_synchronized_across_authoritative_documents() -> None:
    strategy = _read("docs/c_评分设计.md")
    design = _read("docs/b_系统设计.md")
    work = _read("docs/new/g_施工进度.md")

    compact_strategy = "".join(strategy.split())
    assert "每轮每股只请求一次" in compact_strategy
    assert "不维护按候选分排序的储备或失败补位" in compact_strategy
    assert "不恢复每板120只或720只并集上限" in design
    assert "两策略分别保留完整合格集合，物理报价请求取去重并集" in compact_strategy
    assert "candidate_eligibility_before_board_cap" not in work


def test_runtime_has_no_generic_candidate_weight_owner() -> None:
    runtime = _read("src/trader/recommendation/application/pipeline/data_source/source_router.py")
    config = _read("config/strategy.json")
    settings = _read("src/trader/recommendation/infra/settings/models.py")
    performance = _read("src/trader/entrypoints/performance.py")

    assert "_CANDIDATE_WEIGHTS" not in runtime
    assert '"candidate_weights"' not in config
    assert "candidate_weights: Mapping[str, float]" not in settings
    assert "from trader.recommendation.domain.selection.ranking import candidate_score" not in performance
    assert "candidate_score(" not in performance
    assert "_CANDIDATE_WEIGHTS" not in performance
    assert "trader.recommendation.application.pipeline.candidate_pool.candidate_builder import" in performance
    assert "BuildCandidatePlans" in performance
    assert '"board_preselection"' in performance
    assert '"candidate_union_projection"' in performance
    assert "CandidatePlanSet.physical_union" in performance
    assert '"candidate_rows": 5500' in _read("config/performance.json")


def test_candidate_quote_cache_is_sized_and_wired_for_the_physical_union() -> None:
    bootstrap = _read("src/trader/bootstrap.py")
    cache_config = json.loads(_read("config/cache.json"))

    assert 'datasets["candidate_quotes"].capacity' in bootstrap
    assert cache_config["cache_policy"]["datasets"]["candidate_quotes"]["capacity"] == 12000
