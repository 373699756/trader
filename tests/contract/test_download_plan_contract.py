from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _work() -> str:
    return (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")


def test_download_plan_contains_only_the_remaining_dependency_route() -> None:
    work = _work()
    ordered = (
        "## 2. 当前执行章节：V3 训练工件重建与整组发布",
        "## 3. V3 工程运行验收",
        "## 4. 点时证据修复",
        "## 5. 候选容量与排序历史验证",
        "## 6. 一次性终端留出与 Shadow",
        "## 7. 人工候选策略生产启用",
    )
    positions = tuple(work.index(item) for item in ordered)
    assert positions == tuple(sorted(positions))
    assert "`completed`" not in work
    assert "baostock_daily_archive" not in work


def test_monthly_snapshot_plan_is_idempotent_and_fails_closed_on_missing_facts() -> None:
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    work = _work()

    for token in (
        "control.sqlite3",
        "partitions/YYYY/MM.sqlite3",
        "(trade_date, code, revision_id)",
        "重复内容幂等",
        "不同内容写新 revision",
        "活动指针不因",
    ):
        assert token in design
    for token in (
        "effective_at",
        "published_at",
        "禁止当前快照回填历史",
    ):
        assert token in design
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work


def test_training_and_runtime_acceptance_cannot_auto_promote() -> None:
    work = _work()
    section = work[work.index("## 2. 当前执行章节：V3") : work.index("## 8.")]

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


def test_current_training_task_uses_the_reviewed_zero_argument_history_prerequisite() -> None:
    work = _work()

    for required in (
        "第 12.7 节阶段 A–G",
        "阶段 A 至 F 已完成",
        "阶段 G 已完成代码切换与发布门禁收尾",
        "最低 10 GiB",
        "行业上下文收敛为最近完整交易日快照",
        "20:30 前不再请求尚未发布的当日日线",
        "高效日更来源保持阻塞",
    ):
        assert required in work
