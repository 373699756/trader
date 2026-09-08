from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_download_plan_tracks_completed_daily_archive_and_next_industry_batch() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("## 4. 历史下载与行业补全执行计划") :]
    daily = section[section.index("### 4.1") : section.index("### 4.2")]
    industry = section[section.index("### 4.2") : section.index("### 4.3")]
    training = section[section.index("### 4.3") : section.index("### 4.4")]
    production = section[section.index("### 4.4") :]

    assert not (ROOT / "docs/download.md").exists()
    assert "计划整体状态：第一批 `completed`，第二批 `pending`" in section
    assert "第一批：完成 2000 日日线正式归档" in daily
    assert "状态：`completed`" in daily
    assert "第二批：独立补齐历史行业事实" in industry
    assert "状态：`pending`" in industry
    assert "第三批：冻结训练输入并验证收益" in training
    assert "状态：`blocked_by_industry_history`" in training
    assert "第四批：Shadow 与生产启用" in production
    assert "状态：`blocked_by_training_validation`" in production


def test_industry_repair_plan_preserves_daily_archive_identity() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = work[work.index("### 4.2 第二批：独立补齐历史行业事实") :]

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
    section = work[work.index("### 4.3 第三批：冻结训练输入并验证收益") :]

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
