from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _compact(relative: str) -> str:
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def test_unfinished_roadmap_orders_data_before_training_and_activation() -> None:
    work = _compact("docs/03_工程实施.md")
    ordered = (
        "dynamic_cutoff_and_missing_fact_acquisition",
        "v3_training_artifact_rebuild",
        "v3_runtime_acceptance",
        "point_in_time_evidence_remediation",
        "terminal_holdout_and_shadow",
        "manual_candidate_strategy_activation",
    )
    first_positions = tuple(work.index(item) for item in ordered)
    assert first_positions == tuple(sorted(first_positions))


def test_roadmap_keeps_point_in_time_and_manual_authority_fail_closed() -> None:
    strategy = _compact("docs/01_评分逻辑.md")
    work = _compact("docs/03_工程实施.md")

    for token in (
        "不得用 15:00 收盘",
        "未来 collector",
        "fixture",
        "terminal_holdout",
        "用户在独立批次明确授权",
        "禁止自动训练、晋级、激活或回退",
    ):
        assert token in work
    assert "所有评分策略验证只使用历史 point-in-time 数据" in strategy


def test_today_and_d25_model_heads_remain_separately_authorized() -> None:
    work = _compact("docs/03_工程实施.md")

    assert "today_model_head" in work
    assert "d25_model_head" in work
    assert "这两项都不由普通“继续”触发" in work
    assert "D25 保持一个 2–5 日模型头" in work
