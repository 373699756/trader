from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _work() -> str:
    return (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")


def test_download_plan_contains_only_the_remaining_dependency_route() -> None:
    work = _work()
    ordered = (
        "## 2. 当前执行章节：动态截止日与缺失事实补采",
        "## 3. 候选单一所有权当前验收复核",
        "## 4. 生产权重配置单一来源",
        "## 5. V3 训练工件重建与整组发布",
        "## 6. V3 工程运行验收",
        "## 7. 点时证据修复",
        "## 8. 候选容量与排序历史验证",
        "## 9. 一次性终端留出与 Shadow",
        "## 10. 人工候选策略生产启用",
    )
    positions = tuple(work.index(item) for item in ordered)
    assert positions == tuple(sorted(positions))
    assert "`completed`" not in work
    assert "baostock_daily_archive" not in work


def test_increment_plan_preserves_parent_and_fails_closed_on_missing_facts() -> None:
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    work = _work()
    section = work[work.index("## 2. 当前执行章节：动态截止日") : work.index("## 3. 候选单一所有权")]

    for token in ("父 manifest 原字节不变", "同一 key 同内容幂等", "不同内容", "失败关闭"):
        assert token in design
    for token in (
        "effective_at",
        "published_at",
        "禁止用当前快照回填历史",
    ):
        assert token in section


def test_training_and_runtime_acceptance_cannot_auto_promote() -> None:
    work = _work()
    section = work[work.index("## 5. V3") : work.index("## 11.")]

    for token in (
        "一次原子切换",
        "重新训练而不是给旧 schema 补字段或放宽 loader",
        "historical_data_insufficient",
        "point_in_time_parity=false",
        "production_authority=false",
        "默认 V1 不变",
        "用户在独立批次明确授权",
    ):
        assert token in section


def test_plan_does_not_publish_an_extra_research_status_command() -> None:
    assert "./run.sh research-status" not in _work()
