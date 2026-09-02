from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_baostock_history_plan_freezes_2000_row_scope_and_four_owner_boundaries() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]

    for required in (
        "score_baostock_daily_core_v2",
        "每只股票最多 2000 个代码-日期逻辑记录",
        "`--sessions` 接受 1–2000 且默认 2000",
        "最近 2000 个交易所开市日",
        "2026-08-31",
        "前复权",
        "未复权",
        "同一行",
        "production_authority=false",
        "Codex A",
        "Codex B",
        "Codex C",
        "Codex D",
        "11:20",
        "14:50",
        "不得",
    ):
        assert required in section
    assert "score_baostock_daily_core_v2" in design
    assert "research-baostock-history" in design
    assert "--sessions 2000" in design
    assert "计划中" in design
    assert "最近 1500" not in section
    assert "--sessions 1500" not in section
    assert "score_baostock_daily_core_v1" not in section


def test_baostock_plan_does_not_treat_recent_ipos_as_missing_2000_day_rows() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]

    assert "上市日" in section
    assert "应有交易日" in section
    assert "新上市股票" in section
    assert "伪造" in section


def test_baostock_plan_has_one_data_owner_and_fixed_operational_caps() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]

    for required in (
        "Codex A 独占数据内容语义",
        "Codex B 不实现下载、覆盖审计或切分",
        "Codex C 不定义或重切数据集",
        "Codex D 不决定覆盖是否通过",
        "最多 2 个进程",
        "单次供应商调用墙钟上限 60 秒",
        "最多重试 2 次",
        "每进程每秒最多 1 次查询",
        "全体和逐板应有代码-日期单元覆盖率均不低于 95%",
    ):
        assert required in section


def test_baostock_plan_is_the_only_next_action_before_v3_training() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")

    assert "唯一下一执行章节：15.1.38" in strategy
    assert "15.1.35 | `blocked_by_15_1_38`" in strategy
    assert "15.1.37 | `control_only`" in strategy


def test_codex_c_baostock_holdout_isolation_contract_is_implemented_without_opening_holdout() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")
    source = ROOT / "src" / "trader" / "domain" / "research" / "baostock_holdout_isolation.py"
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]

    assert source.is_file()
    assert "Codex C 工程契约已完成" in section
    assert "baostock_holdout_isolation_contract" in section
    assert "score_tomorrow_historical_candidate_v1" in section
    assert "tomorrow_v3_point_in_time_holdout_v1" in section
    assert "不打开留出" in section
    assert "production_authority=false" in section
    assert "baostock_holdout_isolation_contract" in design
    assert "terminal_holdout_opened=false" in design


def test_codex_b_wave_one_has_a_read_only_hash_bound_input_contract() -> None:
    strategy = (ROOT / "docs" / "recommendation-strategy.md").read_text(encoding="utf-8")
    design = (ROOT / "docs" / "software-business-design.md").read_text(encoding="utf-8")
    section = strategy[strategy.index("#### 15.1.38") : strategy.index("### 15.2")]
    domain_contract = ROOT / "src" / "trader" / "domain" / "research" / "tomorrow_v3_input_compatibility.py"
    application_contract = ROOT / "src" / "trader" / "application" / "research" / "tomorrow_v3_input_compatibility.py"

    assert "Codex B 波次 1 状态：已完成" in section
    assert "tomorrow_v3_input_compatibility_v1" in section
    assert "15.1.38 整节仍为 `pending`" in section
    assert "tomorrow_v3_input_compatibility_v1" in design
    assert domain_contract.is_file()
    assert application_contract.is_file()


def test_baostock_runtime_keeps_retry_rate_timeout_and_cancel_caps_executable() -> None:
    runtime = (ROOT / "src/trader/infra/research/baostock_history_runtime.py").read_text(encoding="utf-8")
    gateway = (ROOT / "src/trader/infra/research/baostock_daily.py").read_text(encoding="utf-8")

    assert "ProcessPoolExecutor" not in runtime
    assert "BAOSTOCK_CANCEL_GRACE_SECONDS = 10.0" in runtime
    assert "BAOSTOCK_QUERY_INTERVAL_SECONDS = 1.0" in runtime
    assert "request.retries" in runtime
    assert "request.timeout_seconds" in runtime
    assert ".terminate()" in runtime
    assert ".get_data(" not in gateway
