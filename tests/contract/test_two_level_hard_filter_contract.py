from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_authoritative_documents_define_two_level_filter_before_h1_download() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")

    required_strategy = (
        "一级永久资格过滤",
        "二级动态硬过滤",
        "historical_audited_loss",
        "historical_st",
        "qualification_pending",
        "普通新闻",
        "不得创建一级永久事实",
        "事实生效时间",
        "不得用当前一级名单反向删除",
        "全市场批量接口",
        "DeepSeek",
    )
    required_design = (
        "IssuerEligibilityRegistry",
        "历史预热",
        "候选定向行情",
        "逐股公司研究",
        "分钟行情",
        "一级资格",
        "permanently_excluded",
        "eligible_unverified",
    )

    for token in required_strategy:
        assert token in strategy
    for token in required_design:
        assert token in design


def test_h1_roadmap_depends_on_completed_two_level_filter_section() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")

    level_one = strategy.index("一级永久资格名单与二级硬过滤")
    h1 = strategy.index("H1 分策略历史归档与点时覆盖审计")

    assert level_one < h1
    assert "状态：已完成" in strategy[level_one:h1]
