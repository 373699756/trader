from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/trader"


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_historical_only_score_validation_is_the_authoritative_route() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    design = _compact(ROOT / "docs/software-business-design.md")

    for token in (
        "所有评分策略验证只使用历史 point-in-time 数据",
        "historical_data_insufficient",
        "historical_rejected",
        "historical_validated",
        "合法空仓日",
        "production_authority=false",
        "线上 T+1 结算只用于正式推荐历史与运行监控",
    ):
        assert token in strategy
    for token in (
        "评分验证唯一使用历史 point-in-time 回放",
        "research_readiness",
        "线上 T+1 outcome 只保存正式推荐历史和运行监控",
    ):
        assert token in design


def test_forward_score_validation_owners_and_commands_are_retired() -> None:
    retired_paths = (
        SOURCE / "application/research/score_r5.py",
        SOURCE / "application/research/score_r7.py",
        SOURCE / "application/research/preregistered_shadow.py",
        SOURCE / "application/research/tomorrow_profile_comparison.py",
        SOURCE / "application/research/tomorrow_profile_reporting.py",
        SOURCE / "application/research/tomorrow_profile_settlement.py",
        SOURCE / "domain/research/tomorrow_profile_comparison.py",
        SOURCE / "infra/research/forward_evidence.py",
        SOURCE / "infra/research/score_r7_artifacts.py",
        SOURCE / "infra/persistence/tomorrow_profile_comparison.py",
    )
    assert not [path for path in retired_paths if path.exists()]

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SOURCE / "entrypoints/cli.py",
            ROOT / "run.sh",
            ROOT / "run.ps1",
        )
    )
    assert "research-r7-dossier" not in combined
    assert "research-tomorrow-profile-report" not in combined


def test_remaining_offline_research_is_historical_and_production_isolated() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    design = _compact(ROOT / "docs/software-business-design.md")
    work = _compact(ROOT / "docs/work.md")

    for token in (
        "score_r6_historical_legacy",
        "score_r6_daily_trend",
        "score_r6_daily_stability",
        "score_tomorrow_historical_p2",
        "tomorrow_v1_v2_h0_holdout_report_v2",
        "tomorrow_v2_historical_risk_probability_v1",
    ):
        assert token in work or token in design
    assert "旧 H0 历史归档、回测和筛选命令已经退役" in design
    assert "download_history" in design
    assert "train-tomorrow" in design
    assert "不得接入生产组合根或在线请求链" in design


def test_p2_historical_rejection_and_manual_production_override_remain_explicit() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")

    for token in (
        "daily_reconstructible_ensemble_v1",
        "single_candidate_pass_or_stop",
        "score_h0_ohlcv_cross_section",
        "historical_rejected",
        "manual_user_override",
        "automatic_t1_outcome_settlement",
        "automatic_model_update=false",
        "loss_probability_status=not_modeled",
        "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5",
    ):
        assert token in strategy


def test_v1_v2_historical_evidence_does_not_create_a_running_collection_gate() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    design = _compact(ROOT / "docs/software-business-design.md")
    work = _compact(ROOT / "docs/work.md")

    for token in (
        "不能据此断言 V2 未来更能挣钱",
        "V2 的平均成本后净增量证据强于 V1",
        "tomorrow_v1_v2_h0_holdout_report_v2",
        "不设跨年配对采集任务",
    ):
        assert token in strategy or token in work
    for retired in ("522 个有效交易日", "tomorrow_v1_v2_paired_forward_v1"):
        assert retired not in strategy
        assert retired not in design


def test_tomorrow_zero_score_is_explained_as_a_cost_aware_cash_result() -> None:
    strategy = _compact(ROOT / "docs/recommendation-strategy.md")
    design = _compact(ROOT / "docs/software-business-design.md")

    for token in (
        "`no_positive_net_utility`",
        "预测成本后净效用均不大于 0",
        "合法空仓结果",
        "不得为了显示非零分数",
    ):
        assert token in strategy
    for token in (
        "模型预测成本后净超额均未转正",
        "按固定成本规则信号分为 0",
        "不能误报成数据异常",
    ):
        assert token in design
