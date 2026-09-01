from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/trader"


def test_authoritative_contracts_make_history_the_only_score_validation_source() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())

    for expected in (
        "所有评分策略验证只使用历史 point-in-time 数据",
        "合法空仓日",
        "historical_data_insufficient",
        "60 日训练、20 日校准、40 日独立检验均从已封存历史日期取得",
        "线上 T+1 结算只用于正式推荐历史与运行监控",
    ):
        assert expected in strategy
    assert "评分验证唯一使用历史 point-in-time 回放" in design

    for retired in (
        "forward_collecting",
        "score_r5_forward_day",
        "score_r6_forward_",
        "tomorrow_v1_v2_paired_forward",
        "等待 20 个未来",
        "前向 collector",
    ):
        assert retired not in strategy
        assert retired not in design


def test_production_tree_has_no_forward_score_validation_owner() -> None:
    retired_paths = (
        SOURCE / "domain/research/tomorrow_profile_comparison.py",
        SOURCE / "application/research/tomorrow_profile_comparison.py",
        SOURCE / "application/research/tomorrow_profile_reporting.py",
        SOURCE / "application/research/tomorrow_profile_settlement.py",
        SOURCE / "infra/persistence/tomorrow_profile_comparison.py",
        SOURCE / "infra/persistence/tomorrow_profile_comparison_codec.py",
        SOURCE / "infra/research/forward_evidence.py",
        SOURCE / "infra/research/preregistered_shadow_artifacts.py",
    )
    assert not [path for path in retired_paths if path.exists()]

    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SOURCE / "bootstrap.py",
            SOURCE / "bootstrap_status.py",
            SOURCE / "application/market_data/v2_input_runtime.py",
            SOURCE / "application/ports/v2_runtime.py",
            SOURCE / "web/api/routes.py",
        )
    )
    for retired in (
        "TomorrowProfileComparator",
        "TomorrowProfileSettlementService",
        "tomorrow_profile_comparison",
        "tomorrow_profile_research_input",
    ):
        assert retired not in production


def test_online_outcomes_remain_monitoring_only_without_changing_scoring_or_freeze() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    bootstrap = (SOURCE / "bootstrap.py").read_text(encoding="utf-8")

    for invariant in (
        "local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty",
        "11:20",
        "14:50",
        "automatic_model_update=false",
    ):
        assert invariant in strategy
    assert "OutcomeSettlementService" in bootstrap
    assert "TomorrowProductionModelScoringService" in bootstrap
