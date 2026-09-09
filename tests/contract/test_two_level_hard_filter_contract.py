from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_authoritative_documents_define_two_level_filter_before_h1_download() -> None:
    strategy = (ROOT / "docs" / "01_评分逻辑.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "02_工程设计.md").read_text(encoding="utf-8")

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


def test_historical_aliases_are_retired_from_the_unfinished_plan() -> None:
    strategy = (ROOT / "docs" / "01_评分逻辑.md").read_text(encoding="utf-8")
    work = (ROOT / "docs" / "03_工程实施.md").read_text(encoding="utf-8")

    level_one = strategy.index("一级永久资格过滤")
    assert level_one >= 0
    assert "15.1.35" not in work
    assert "15.1.21–15.1.34" not in work
    assert "candidate_eligibility_before_board_cap" in work
    assert "二级动态硬过滤" in strategy
