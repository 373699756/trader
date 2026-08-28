from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_next_score_diagnostic_gate_is_owned_by_authoritative_docs() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())

    assert "原生评分因子诊断层" in design
    for statement in (
        "score_native_factor_diagnostics_v1",
        "score_factor_diagnostic_report_v1",
        "14:50 至下一交易日收盘净超额",
        "总体 Pearson",
        "总体 Spearman",
        "ICIR",
        "Q5-Q1",
        "相隔 1/3/5 个已观察交易日",
        "small/mid/large",
        "20/50/100bp 平均净超额",
        "MAE/ATR20 <= -1.5",
        "最大单股占比",
        "前五只合计占比",
        "MAE/ATR20",
        "剪枝前 oracle recall",
        "production_authority=false",
        "不产生生产晋级权限",
    ):
        assert statement in strategy

    bootstrap = (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")
    assert "factor_diagnostics" not in bootstrap
    assert "ScoreNativeFactorDiagnostics" not in bootstrap


def test_score_plan_p0_pre_registration_is_reflected_in_authoritative_docs() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    design_flat = " ".join(design.split())

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
    for statement in (
        "完整点时股票池只允许保存代码、板块、行业、历史 ST",
        "不保存简称、硬拒绝分数、未来收益",
        "每个 `input_version`",
        "只在 local 观察保存一次完整人口",
        "hybrid 只引用同一人口哈希",
    ):
        assert statement in strategy
    for statement in (
        "score_p0_v2",
        "2026-08-21（含）至 2026-10-23（含）",
        "2026-10-26（含）至 2026-11-20（含）",
        "bootstrap_master_seed = 20260820",
        "不得以前序日期替换失败日",
    ):
        assert statement in strategy

    for statement in (
        "score_h0_v1",
        "640",
        "至少 66 根",
        "2024-07-01",
        "2025-12-31",
        "2026-01-01",
        "2026-07-31",
        "0.50 * momentum_rank + 0.30 * stability_rank + 0.20 * liquidity_rank",
        "ohlcv_cross_section_v1",
        "逐股历史内容哈希",
        "不能生成 `promotion_eligible`",
    ):
        assert statement in " ".join(strategy.split())

    for statement in (
        "Score-R0 至 Score-R5 的工程能力已完成",
        "研究链与活动运行库物理分离",
        "不建立第二套行情、评分、冻结、Web 或 DeepSeek 请求链",
        "`historical_collection_failed`",
        "最大只能达到 37/40",
        "2026-08-24、2026-08-25、2026-08-26",
        "策略定义只以荐股策略文档第 15.1 节为准",
    ):
        assert statement in design_flat


def test_score_plan_p1_compact_trace_contract_is_reflected_in_authoritative_docs() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    assert "Score-R0 至 Score-R5 的工程能力已完成" in design
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
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

    for statement in (
        "只消费成功提交后的 `V2DecisionCommitted`",
        "独立 SQLite 研究库",
        "不重新读取行情、重新评分或重新调用模型",
        "审计写入失败不回滚或阻塞正式决策",
        "历史数据不从旧 snapshot 或 shadow 运行库回填",
    ):
        assert statement in design
    for statement in (
        "`v2_committed_research_audit_v2`",
        "`v2_research_committed_event_v2`",
        "既有两个 v1 schema 只允许按原始载荷形状验证哈希并只读解析",
        "`production_local`",
        "`research_shadow`",
        "独立 SHA-256",
        "DeepSeek 物理 HTTP 请求增量必须为 0",
    ):
        assert statement in strategy


def test_score_r2_historical_extraction_contract_is_complete() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")

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
        "Score-R0 至 Score-R5 的工程能力已完成",
        "不接入组合根、HTTP 或生产调度",
        "Polars 不可变 Parquet 分区",
        "不得从当前供应商响应回填",
    ):
        assert statement in design


def test_score_r3_baseline_replay_contract_is_complete() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy_flat = " ".join(strategy.split())
    design_flat = " ".join(design.split())

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
        "Score-R0 至 Score-R5 的工程能力已完成",
        "不接入组合根、HTTP 或生产调度",
        "不能宣称已经取得 40 日收益证据",
    ):
        assert statement in design_flat


def test_score_r4_five_challenger_contract_is_complete() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy_flat = " ".join(strategy.split())
    design_flat = " ".join(design.split())

    for statement in (
        "score_r4_preregistered_parameters_v1",
        "continuous_entry_v1",
        "coverage_shrink_v1",
        "candidate_upper_bound_v1",
        "heat_weak_structure_v1",
        "combined_v1",
        "production、local-only、hybrid",
        "DeepSeek 物理 HTTP 请求增量必须为 0",
    ):
        assert statement in strategy_flat
    for statement in (
        "R4 五个 独立研究挑战者",
        "local-only/hybrid 同日同股配对 manifest",
        "任何变更必须建立新的研究版本",
    ):
        assert statement in strategy_flat
    for statement in (
        "Score-R0 至 Score-R5 的工程能力已完成",
        "不增加 DeepSeek HTTP",
        "不执行 R5 统计或晋级",
    ):
        assert statement in design_flat


def test_score_r5_statistical_gate_and_forward_contract_is_complete() -> None:
    strategy = (ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/software-business-design.md").read_text(encoding="utf-8")
    strategy_flat = " ".join(strategy.split())
    design_flat = " ".join(design.split())

    for statement in (
        "score_r5_statistical_gate_v1",
        "score_r5_paired_mbb_holm_v1",
        "score_r5_forward_day_v1",
        "score_r5_final_report_v1",
        "同键同内容重放幂等、不同内容冲突",
        "hybrid 相对 local-only",
        "不得生成伪 `promotion_eligible`",
    ):
        assert statement in strategy_flat
    for statement in (
        "Score-R0 至 Score-R5 的工程能力已完成",
        "固定五变体 Holm 家族",
        "当前真实 历史覆盖不足 40 日",
        "活动生产策略保持不变",
    ):
        assert statement in design_flat


def test_score_r6_parameter_and_forward_gate_contract_is_complete() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())

    for statement in (
        "score_r6_historical_v1",
        "momentum/stability/liquidity",
        "76/78/80",
        "3/4/5",
        "lambda` 固定为 0/25%/50%",
        "至少 5000 个股日",
        "score_r6_historical_report_v1",
        "score_r6_forward_*",
        "不得与 `score_p0_v1` 或 `score_p0_v2`",
        "生产范围固定为 `local_only`",
    ):
        assert statement in strategy
    for statement in (
        "research-r6-screen",
        "runtime_dir/score-r6",
        "三板小样本统一回退全局参数",
        "score_r6_promotion_executable",
        "Score-R7 只生成待人工审查档案",
    ):
        assert statement in design


def test_score_r7_dossier_contract_is_complete_without_authorizing_production() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())

    for statement in (
        "score_r7_promotion_dossier_v1",
        "20/50/100bp × 3/5/10 日",
        "manual_review_status=pending",
        "production_change_authorized=false",
        "不得写活动配置",
    ):
        assert statement in strategy
    for statement in (
        "research-r7-dossier",
        "runtime_dir/score-r7",
        "复算结果与已封存 R6 报告哈希一致",
        "不启动生产发布",
    ):
        assert statement in design


def test_score_r6_daily_trend_contract_is_preregistered_without_production_authority() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())

    for statement in (
        "score_r6_daily_trend_v1",
        "30/25/20/15/10",
        "共 48 个候选",
        "单板最多 4 只",
        "至少高 0.10 个百分点",
        "score_r6_daily_trend_report_v1",
        "没有生产晋级权限",
    ):
        assert statement in strategy
    for statement in (
        "research-r6-daily-screen",
        "runtime_dir/score-r6-daily",
        "不访问网络",
        "不能写活动配置",
    ):
        assert statement in design


def test_score_r6_stability_contract_freezes_turnover_mechanisms_without_production_authority() -> None:
    strategy = " ".join((ROOT / "docs/recommendation-strategy.md").read_text(encoding="utf-8").split())
    design = " ".join((ROOT / "docs/software-business-design.md").read_text(encoding="utf-8").split())

    for statement in (
        "score_r6_daily_stability_v1",
        "rank_persistence_bonus",
        "previous_score_weight",
        "entrant_turnover_penalty",
        "共 26 个候选",
        "平均换手至少降低 0.03",
        "reused_observed_validation_window",
        "没有生产晋级权限",
    ):
        assert statement in strategy
    for statement in (
        "research-r6-stability-screen",
        "runtime_dir/score-r6-stability",
        "不访问网络",
        "不能写活动配置",
    ):
        assert statement in design
