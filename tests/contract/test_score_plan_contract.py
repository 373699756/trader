from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_score_plan_p0_pre_registration_is_reflected_in_authoritative_docs() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    design_flat = " ".join(design.split())

    assert "### Score-R0：权威契约与预注册（已完成）" in plan
    assert "V2 工程发布章节全部闭合" in plan
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
        "Score-R0 至 Score-R3 已完成",
        "研究链与活动运行库物理分离",
        "不建立第二套行情、评分、冻结、Web 或 DeepSeek 请求链",
        "策略定义只以荐股策略文档第 15.1 节为准",
    ):
        assert statement in design_flat


def test_score_plan_p1_compact_trace_contract_is_reflected_in_authoritative_docs() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "### Score-R1：紧凑决策轨迹（已完成）" in plan
    assert "Score-R0 至 Score-R3 已完成" in design
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


def test_score_r1_migrate_committed_audit_contract_is_complete() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "### Score-R1-Migrate：迁移到 V2 committed event（已完成）" in plan
    for statement in (
        "只消费成功提交后的 `V2DecisionCommitted`",
        "独立 SQLite 研究库",
        "不重新读取行情、重新评分或重新调用模型",
        "审计写入失败不回滚或阻塞正式决策",
        "历史数据不从旧 snapshot 或 shadow 运行库回填",
    ):
        assert statement in design
    for statement in (
        "`v2_committed_research_audit_v1`",
        "`v2_research_committed_event_v1`",
        "`production_local`",
        "`research_shadow`",
        "独立 SHA-256",
        "DeepSeek 物理 HTTP 请求增量必须为 0",
    ):
        assert statement in strategy


def test_score_r2_historical_extraction_contract_is_complete() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "### Score-R2：最多 40 日历史点时数据（已完成）" in plan
    assert "下一研究章节为 Score-R4" in " ".join(plan.split())
    for statement in (
        "score_r2_historical_v1",
        "score_r2_partition_v1",
        "每板生产 Top120 身份作为起始集",
        "Top6、板块 60% 和每行业最多 2 只约束",
        "相同身份同内容重放幂等，不同内容冲突",
        "不足 40 日的顶层状态固定为 `exploratory`",
    ):
        assert statement in strategy
    for statement in (
        "Score-R0 至 Score-R3 已完成",
        "不接入组合根、HTTP 或生产调度",
        "Polars 不可变 Parquet 分区",
        "不得从当前供应商响应回填",
    ):
        assert statement in design


def test_score_r3_baseline_replay_contract_is_complete() -> None:
    plan = (ROOT / "docs/implementation-plan.md").read_text(encoding="utf-8")
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy_flat = " ".join(strategy.split())
    design_flat = " ".join(design.split())

    assert "### Score-R3：基线回放与报告（已完成）" in plan
    assert "下一研究章节为 Score-R4" in " ".join(plan.split())
    for statement in (
        "score_r3_baseline_report_v1",
        "20bp、50bp、100bp",
        "平均 MAE/ATR20",
        "候选召回率",
        "字段覆盖率",
        "平均日内 Spearman Rank IC",
        "相同内容重放幂等，不同内容冲突",
        "exploratory",
    ):
        assert statement in strategy_flat
    for statement in (
        "Score-R0 至 Score-R3 已完成",
        "不接入组合根、HTTP 或生产调度",
        "不能宣称已经取得 40 日收益证据",
    ):
        assert statement in design_flat
