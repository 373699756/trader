from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_authoritative_documents_define_st_prefilter_and_two_level_runtime_filter() -> None:
    strategy = (ROOT / "docs" / "1_评分逻辑.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "2_工程设计.md").read_text(encoding="utf-8")

    required_strategy = (
        "ST 预过滤",
        "一级市场条件过滤",
        "二级公司资格与风险过滤",
        "historical_audited_loss",
        "latest_disclosed_report_loss",
        "historical_st",
        "qualification_pending",
        "普通新闻",
        "不创建永久排除事实",
        "事实生效时间",
        "不得用当前名单反向删除",
        "全市场批量接口",
        "ST 预过滤后的全部受支持 A 股",
        "只对缺失或过期分项联网补缺",
        "DeepSeek",
    )
    required_design = (
        "SQLiteIssuerEligibilityIndex",
        "历史投影",
        "候选定向行情",
        "逐股公司研究",
        "分钟行情",
        "ST预过滤",
        "一级市场过滤",
        "二级公司风险过滤",
        "data/risk/YYYY-MM-DD.sqlite3",
        "permanently_excluded",
        "eligible_unverified",
    )

    for token in required_strategy:
        assert token in strategy
    for token in required_design:
        assert token in design


def test_retired_filter_labels_and_historical_aliases_are_absent() -> None:
    strategy = (ROOT / "docs" / "1_评分逻辑.md").read_text(encoding="utf-8")
    work = (ROOT / "docs" / "3_工程实施.md").read_text(encoding="utf-8")

    assert "一级永久资格过滤" not in strategy
    assert "二级动态硬过滤" not in strategy
    assert "15.1.35" not in work
    assert "15.1.21–15.1.34" not in work
    assert "candidate_eligibility_before_board_cap" not in work
    assert "baostock_increment_archive" not in work
    assert "二级公司资格与风险过滤" in strategy
