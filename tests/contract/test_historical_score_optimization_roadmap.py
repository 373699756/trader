from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compact(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def test_unfinished_roadmap_orders_data_before_training_and_activation() -> None:
    work = _compact("docs/03_工程实施.md")
    ordered = (
        "v3_shared_multi_target_samples",
        "v3_today_model_head",
        "v3_d25_model_head",
        "v3_unified_training_entrypoint",
        "v3_multihead_runtime_scoring",
        "v3_multihead_delivery_and_freeze",
        "v3_real_full_training",
        "v3_explicit_chain_acceptance",
    )
    first_positions = tuple(work.index(item) for item in ordered)
    assert first_positions == tuple(sorted(first_positions))
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work


def test_roadmap_keeps_point_in_time_and_manual_authority_fail_closed() -> None:
    strategy = _compact("docs/01_评分逻辑.md")
    work = _compact("docs/03_工程实施.md")

    for token in (
        "不新增分钟库",
        "15:00 收盘代理",
        "historical_data_insufficient",
        "production_authority=false",
        "automatic_model_update=false",
        "默认 `scoring_profile=v1` 不变",
    ):
        assert token in work
    assert "所有评分策略验证只使用历史 point-in-time 数据" in strategy


def test_today_and_d25_model_heads_remain_separately_authorized() -> None:
    work = _compact("docs/03_工程实施.md")

    assert "v3_today_model_head" in work
    assert "v3_d25_model_head" in work
    assert "用户已明确授权" in work
    assert "D25 使用与 Today 相同的五个趋势特征" in work
