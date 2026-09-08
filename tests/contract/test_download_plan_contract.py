from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_download_plan_is_merged_with_explicit_unfinished_batch_states() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("## 4. 历史数据与训练执行计划") :]

    assert not (ROOT / "docs/download.md").exists()
    for required in (
        "计划整体状态：`pending`",
        "### 4.1 `baostock_daily_archive`",
        "状态：`pending`",
        "### 4.2 `historical_industry_facts`",
        "状态：`blocked_by_baostock_daily_archive`",
        "### 4.3 `tomorrow_training_validation`",
        "状态：`blocked_by_historical_industry_facts`",
        "### 4.4 `shadow_and_production_activation`",
        "状态：`blocked_by_tomorrow_training_validation`",
    ):
        assert required in section


def test_download_plan_does_not_publish_an_extra_research_status_command() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")

    assert "./run.sh research-status" not in work
    assert "./run.sh check" in work


def test_industry_repair_plan_preserves_daily_archive_identity() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("### 4.2 `historical_industry_facts`") :]

    for required in (
        "不能把当前行业直接当作历史行业",
        "只写行业事实和训练事实 checkpoint",
        "不得修改 `daily_cells`、`code_batches`",
        "独立行业数据集 hash",
        "effective_at",
        "无法证明的日期继续保持未就绪",
        "排除出 V3 训练",
        "时间穿越检查结果",
    ):
        assert required in section


def test_training_comparison_and_shadow_cannot_auto_promote() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("### 4.3 `tomorrow_training_validation`") :]

    for required in (
        "2000 日线归档 hash",
        "行业事实数据 hash",
        "Top10、Top20、Top50",
        "至少 200 个交易日",
        "无行业字段对照",
        "只用于研究",
        "不自动 promotion",
        "用户明确授权",
        "原始日线 hash 保持不变",
    ):
        assert required in section
