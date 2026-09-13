from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _work() -> str:
    return (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")


def test_download_plan_contains_only_the_remaining_dependency_route() -> None:
    work = _work()
    ordered = (
        "## 2. 共享多目标样本管线",
        "## 3. Today V3 模型头",
        "## 4. D25 V3 模型头",
        "## 5. train-v3 统一入口",
        "## 6. V3 多头运行评分",
        "## 7. 状态、API、SSE、Web 和冻结",
        "## 8. 真实全量训练",
        "## 9. 显式 V3 整链验收",
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
    section = work[work.index("## 2. 共享多目标样本管线") : work.index("## 10.")]

    for token in (
        "任一固定工件组失败则所选档位启动失败",
        "historical_data_insufficient",
        "point_in_time_parity=false",
        "production_authority=false",
        "默认 `scoring_profile=v1` 不变",
        "不自动重启",
    ):
        assert token in section


def test_plan_does_not_publish_an_extra_research_status_command() -> None:
    assert "./run.sh research-status" not in _work()


def test_current_training_task_uses_the_reviewed_zero_argument_history_prerequisite() -> None:
    work = _work()

    for required in (
        "当前 `data/history/baostock` 日线",
        "不修改历史 archive schema",
        "不重新下载历史",
        "只完整验证一次 snapshot",
        "计算线程 2",
        "峰值 RSS 2048 MiB",
    ):
        assert required in work
