from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_score_plan_p0_pre_registration_is_reflected_in_authoritative_docs() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "### Score-R0：权威契约与预注册（已完成）" in plan
    assert "下一工程章节为 V2-E11" in plan
    assert "本文是唯一活动施工计划" in plan
    assert "评价最多 60 个不同交易日" in plan
    assert "固定最多 40 日历史和 20 日连续前向" in plan

    assert "Score-R0（评分科学化研究，非生产）已预注册以下固定边界" in strategy
    for statement in (
        "2026-06-15（含）至 2026-08-10（含）",
        "2026-11-02（含）至 2026-11-27（含）",
        "bootstrap_master_seed = 20260811",
        "bootstrap_repetitions = 10000",
        "score_p0_v1|20260811|{variant}|{block_days}",
        "5 日为主区块",
        "3 日和 10 日",
        "Holm",
        "continuous_entry",
        "coverage_shrink",
        "candidate_upper_bound",
        "heat_weak_structure",
        "combined_v1",
        "全程至少形成 300 条",
        "前向阶段至少 100 条",
        "TopK 候选召回率不低于 99%",
        "单只股票不超过全部正向超额收益的 10%",
        "前五只合计不超过 30%",
        "不得新增 DeepSeek 物理 HTTP 请求",
    ):
        assert statement in strategy
    assert "硬拒绝股票代码、简称、逐股事实、分数和未来收益均不得写入研究证据" in strategy

    for statement in (
        "Score-R0 与 Score-R1 已完成权威预注册和紧凑决策轨迹",
        "研究链与活动运行库物理分离",
        "不建立第二套行情、评分、冻结、Web 或 DeepSeek 请求链",
        "策略定义只以荐股策略文档第 15.1 节为准",
    ):
        assert statement in design


def test_score_plan_p1_compact_trace_contract_is_reflected_in_authoritative_docs() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "### Score-R1：紧凑决策轨迹（已完成）" in plan
    assert "Score-R0 与 Score-R1 已完成权威预注册和紧凑决策轨迹" in design
    assert "研究链与活动运行库物理分离" in design
    for statement in (
        "候选字段缺失掩码",
        "生产 Top120 身份",
        "结构化模型风险代码",
        "每个 `input_version` 只保存一条配对轨迹",
        "不得新增 DeepSeek 物理请求",
        "研究轨迹不参与生产排序、动作、 统一决策提交、冻结、API 或收益结算",
    ):
        assert statement in " ".join(strategy.split())
