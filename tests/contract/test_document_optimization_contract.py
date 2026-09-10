from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "docs/01_评分逻辑.md"
DESIGN = ROOT / "docs/02_工程设计.md"
WORK = ROOT / "docs/03_工程实施.md"
REPLAY = ROOT / "docs/04_策略回溯.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_design_has_no_dead_section_references_or_delivery_status_ownership() -> None:
    design = _read(DESIGN)

    for stale in (
        "第 15.1.17–15.1.19 节",
        "第 9.1 节活动下行保护",
        "荐股策略文档第 15.1 节",
        "## 14. 当前交付状态与剩余路线",
        "当前交付状态：current-only 工程与发布门禁验收已闭合",
    ):
        assert stale not in design
    assert "评分逻辑第 7.2 节" in design
    assert "交付状态只由 `03_工程实施.md` 维护" in design


def test_design_declares_four_unique_normative_tables() -> None:
    design = _read(DESIGN)

    for table in (
        "表 R：资源所有权唯一规范",
        "表 T：交易日时间线唯一规范",
        "表 F：失败分类唯一规范",
        "表 A：API schema 唯一规范",
    ):
        assert design.count(table) == 1
    assert "其余章节只能引用表 R/T/F/A" in design
    assert design.count("已经开始纯本地评分的旧任务必须完成并可先发布 local") == 1
    assert design.count("若已有更新 pending，旧任务跳过 DeepSeek") == 1


def test_latest_wins_semantics_are_single_and_non_contradictory() -> None:
    design = _read(DESIGN)

    required = (
        "尚未开始的旧 pending 可以被最新输入替换",
        "已经开始纯本地评分的旧任务必须完成并可先发布 local",
        "若已有更新 pending，旧任务跳过 DeepSeek",
        "新 local 随后通过 sequence/CAS 替换临时 current",
    )
    assert all(value in design for value in required)
    assert "已替代任务不得继续发布、请求 DeepSeek 或形成冻结输入" not in design


def test_strategy_orders_eligibility_before_board_limit_and_explains_v3_industry() -> None:
    strategy = _read(STRATEGY)
    pipeline = strategy[strategy.index("## 2. 端到端荐股链路") : strategy.index("## 3.")]

    assert pipeline.index("逐股评分输入资格") < pipeline.index("每板最多 120 只")
    assert "行业直接评分权重为 0" in strategy
    assert "V3 可以将行业用于风险中性化/残差化" in strategy
    assert "不作为正向行业加分" in strategy


def test_strategy_defines_d25_aggregation_and_challenger_policy() -> None:
    strategy = _read(STRATEGY)

    assert "d25_aggregate = (net_excess_T2 + net_excess_T3 + net_excess_T4 + net_excess_T5) / 4" in strategy
    for required in (
        "仅 Tomorrow 使用 challenger",
        "每日最多 2 次 challenger 物理 HTTP 请求",
        "计入每日 168 次全局物理请求上限",
        "新高风险",
        "动作门槛上下 5 分",
        "方向冲突",
        "证据冲突",
        "下行保护集合",
        "只能保持或降低 primary 置信度",
        "不能提高原始分数、置信度或结论",
        "失败时保留 primary；primary 不可用时保留 local",
    ):
        assert required in strategy


def test_research_settlement_is_reproducible_and_one_shot() -> None:
    strategy = _read(STRATEGY)
    replay = _read(REPLAY)

    for required in (
        "进场价",
        "退出价",
        "同一前复权基准",
        "停牌",
        "涨停",
        "跌停",
        "MAE = min((low[t] / entry_price) - 1)",
        "MAE/ATR20",
        "point_in_time_local_only_equal_weight",
        "20bp 是主评价成本",
        "50bp 是准入稳健性门",
        "100bp 只用于压力测试",
        "候选规范与父 hash 已冻结",
        "terminal_holdout_not_opened",
        "只允许原子地打开一次",
        "terminal_holdout_already_opened",
    ):
        assert required in strategy
        assert required in replay


def test_work_plan_contains_only_unfinished_tasks_and_no_historical_aliases() -> None:
    work = _read(WORK)

    assert "本文件只维护尚未完成的工程任务" in work
    assert work.count("状态：`in_progress`") == 1
    assert "baostock_increment_archive" not in work
    assert "blocked_by_candidate_validation" in work
    assert "blocked_by_shadow_and_user_authorization" in work
    for retired in ("`completed`", "15.1.35", "15.1.36", "15.1.37", "15.1.38"):
        assert retired not in work


def test_public_and_low_level_research_status_commands_are_unambiguous() -> None:
    design = _read(DESIGN)

    assert "./run.sh research-status" not in design
    assert "./run.sh check" in design
    assert "research-status" in design


def test_strategy_document_does_not_own_artifact_hashes() -> None:
    strategy = _read(STRATEGY)

    assert "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5" not in strategy
    assert "详细工件身份、hash 与交付证据统一见 `03_工程实施.md` 和 `CHANGELOG.md`" in strategy


def test_current_historical_evidence_is_not_described_as_a_present_artifact() -> None:
    strategy = _read(STRATEGY)
    compact = " ".join(strategy.split())

    assert "历史审计曾验证这条失败关闭边界" in compact
    assert "初次全量审计当时曾封存正式 manifest" in compact
    assert "本 PC 工作区中的正式 manifest、catalog 和 92 个封存分片当前不可用" in compact
    assert "最终 schema 实物复跑仍未验证" in compact
    assert "当前资格审计已验证这条失败关闭边界" not in compact


def test_work_plan_is_an_unfinished_queue_not_a_second_normative_contract() -> None:
    work = _read(WORK)
    compact = " ".join(work.split())

    for required in (
        "评分逻辑](01_评分逻辑.md)",
        "工程设计](02_工程设计.md)",
        "dynamic_cutoff_and_missing_fact_acquisition",
        "v3_training_artifact_rebuild",
        "terminal_holdout_and_shadow",
        "manual_candidate_strategy_activation",
    ):
        assert required in compact

    assert "clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)" not in work
    assert "/api/status.tomorrow_model.computation" not in work


def test_history_windows_cost_ownership_and_terminal_order_are_unambiguous() -> None:
    strategy = _read(STRATEGY)
    design = _read(DESIGN)
    work = _read(WORK)
    replay = _read(REPLAY)

    assert "V1/V2 使用已封存模型，不要求用户下载 2000 日历史或重新训练" in strategy
    assert "Tomorrow V1/V2/V3 在线推理至少需要 61 个有效 qfq 交易日" in design
    assert "V3 离线训练最多消费 2000 个交易所开市日" in design
    assert "扣成本前的预测超额收益" in strategy
    assert "训练目标不得先扣 20bp 后又由在线门重复扣除" in replay
    assert "不能只用 `非 ST + 当日有交易` 代替完整生产漏斗" in replay
    assert "`historical_validated` 只能由一次性终端留出完整报告产生" in replay

    route = replay[replay.index("## 11. 新优化路线如何形成证据闭环") :]
    ordered = (
        "V3 训练与开发/确认",
        "增量同源计算",
        "风险/成本/不确定性与 DeepSeek 消融",
        "完整选择链冻结",
        "一次性终端留出",
        "Shadow",
    )
    positions = tuple(route.index(item) for item in ordered)
    assert positions == tuple(sorted(positions))
    changelog = _read(ROOT / "CHANGELOG.md")
    assert "CanonicalOutcomeEvaluator" in changelog
    assert "v3_single_cost_ownership" in changelog
    assert "`completed`" not in work


def test_hash_validation_is_limited_to_trust_boundaries() -> None:
    strategy = _read(STRATEGY)
    design = _read(DESIGN)
    work = _read(WORK)

    for required in (
        "Hash 校验只属于信任边界",
        "下游不得对同一字节或同一不可变对象再次规范序列化、重新计算 SHA-256",
        "Hash 不能替代点时、字段、单位、覆盖率",
        "不生成无人消费的",
    ):
        assert required in design or required in work
    assert "进程内下游复用已接纳身份，不重复序列化和校验同一对象" in strategy


def test_design_uses_normative_language_instead_of_delivery_chronology() -> None:
    design = _read(DESIGN)

    for delivery_statement in (
        "旧冷启动预热编排、编号缓存阶段、旧执行模式开关和旧缓存分池已经删除",
        "活动组合根已经接入",
        "统一公开外壳已交付",
        "当前代码仍属于 `Unreleased`",
    ):
        assert delivery_statement not in design
