from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_download_plan_is_merged_with_explicit_unfinished_batch_states() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("## 4. 历史数据、参数研究、V3 训练与实时评分统一路线") :]

    assert not (ROOT / "docs/download.md").exists()
    for required in (
        "计划整体状态：`in_progress`",
        "#### `baostock_daily_archive`",
        "状态：`completed`（2026-09-09）",
        "#### `historical_industry_facts`",
        "状态：`completed: historical_data_insufficient`（2026-09-09）",
        "### 4.7 `tomorrow_v3_training_validation`",
        "状态：`completed: historical_data_insufficient`（2026-09-09）",
        "### 4.10 `shadow_and_manual_activation`",
        "状态：`blocked_by_risk_cost_uncertainty_deepseek`",
    ):
        assert required in section


def test_download_plan_does_not_publish_an_extra_research_status_command() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")

    assert "./run.sh research-status" not in work
    assert "./run.sh check" in work


def test_industry_repair_plan_preserves_daily_archive_identity() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("#### `historical_industry_facts`") :]

    for required in (
        "不能把当前行业直接当作历史行业",
        "只写行业事实和训练事实 checkpoint",
        "不得修改 `daily_cells`、`code_batches`",
        "独立行业数据集 hash",
        "effective_at",
        "无法证明的日期继续保持未就绪",
        "排除出 V3 训练",
        "时间穿越检查结果",
        "日线分片保持只读",
        "合格股票 0",
    ):
        assert required in section


def test_training_comparison_and_shadow_cannot_auto_promote() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("### 4.7 `tomorrow_v3_training_validation`") :]

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
        "15:00_close_proxy",
        "daily_close_engineering_proxy",
        "模型载荷 hash",
    ):
        assert required in section
