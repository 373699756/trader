from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _baostock_section(work: str) -> str:
    return work[work.index("### 4.1 `baostock_daily_archive`") : work.index("### 4.2")]


def test_baostock_history_plan_freezes_one_stable_2000_session_scope() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    section = _baostock_section(work)

    for required in (
        "baostock_daily_core",
        "每只股票最多 2000 个代码-日期逻辑记录",
        "`--sessions` 接受 1–2000 且默认 2000",
        "最近 2000 个交易所开市日",
        "raw/qfq 必须在同一行",
        "production_authority=false",
        "11:20",
        "14:50",
        "不得",
    ):
        assert required in section
    assert "baostock_daily_core" in design
    assert "download_history" in design
    assert "--sessions 2000" in design
    assert "开发工作计划" in design
    assert "最近 1500" not in section
    assert "--sessions 1500" not in section
    assert "score_baostock_daily_core_v1" not in section
    assert "15.1.38" not in section
    assert "Codex A" not in section


def test_baostock_plan_does_not_treat_recent_ipos_as_missing_2000_day_rows() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = _baostock_section(work)

    assert "上市日" in section
    assert "应有交易日" in section
    assert "新上市股票" in section
    assert "补造" in section


def test_baostock_plan_has_one_semantic_task_and_fixed_operational_caps() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = _baostock_section(work)

    for required in (
        "本节是 BaoStock 2000 日日线正式归档的唯一执行定义",
        "1 个下载进程和 1 个 SDK 子进程",
        "单次供应商调用墙钟上限 60 秒",
        "最多重试 2 次",
        "每次查询至少间隔 2 秒",
        "全体和逐板应有代码-日期单元覆盖率均不低于 95%",
    ):
        assert required in section


def test_baostock_plan_remains_pending_but_does_not_override_direct_user_work() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    compact = " ".join(work.split())

    assert "`baostock_daily_archive` 保持 `pending`" in compact
    assert "用户直接授权的新任务优先" in compact
    assert "`tomorrow_v3_training_validation` | `blocked_by_baostock_daily_archive`" in work
    assert "`historical_workstream_boundaries` | `control_only`" in work


def test_baostock_holdout_isolation_contract_is_archived_without_opening_holdout() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    source = ROOT / "src/trader/domain/research/baostock_holdout_isolation.py"

    assert source.is_file()
    assert "baostock_holdout_isolation_contract" in work
    assert "score_tomorrow_historical_candidate" in work
    assert "point_in_time_holdout" in work
    assert "不打开留出" in work
    assert "production_authority=false" in work
    assert "baostock_holdout_isolation_contract" in design
    assert "terminal_holdout_opened=false" in design


def test_training_input_has_a_read_only_hash_bound_contract() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    domain_contract = ROOT / "src/trader/domain/research/tomorrow_training_input.py"
    application_contract = ROOT / "src/trader/application/research/tomorrow_training_input.py"

    assert "tomorrow_training_input" in work
    assert "只读校验" in work
    assert "父 manifest hash" in work
    assert "`baostock_daily_archive` | `pending`" in work
    assert "tomorrow_training_input" in design
    assert domain_contract.is_file()
    assert application_contract.is_file()


def test_baostock_runtime_keeps_retry_rate_timeout_and_cancel_caps_executable() -> None:
    runtime = (ROOT / "src/trader/infra/research/baostock_history_runtime.py").read_text(encoding="utf-8")
    gateway = (ROOT / "src/trader/infra/research/baostock_daily.py").read_text(encoding="utf-8")
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")

    assert "ProcessPoolExecutor" not in runtime
    assert "BAOSTOCK_CANCEL_GRACE_SECONDS = 10.0" in runtime
    assert "BAOSTOCK_QUERY_INTERVAL_SECONDS = 2.0" in runtime
    assert "request.retries" in runtime
    assert "request.timeout_seconds" in runtime
    assert ".terminate()" in runtime
    assert ".get_data(" not in gateway
    assert "单次供应商调用墙钟上限 60 秒" in work
    assert "60 秒只约束单次供应商调用，不约束包含多次正常调用的完整阶段或单股任务" in design


def test_baostock_runtime_contract_has_one_observability_owner() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    section = _baostock_section(work)

    for required in (
        "baostock_runtime_progress",
        "preflight",
        "supplier_login",
        "trading_calendar",
        "security_universe",
        "database_initializing",
        "worker_starting",
        "downloading",
        "merging",
        "sessions",
        "universe_count",
        "checkpointed_codes",
        "remaining_codes",
        "completed_codes",
        "failed_codes",
        "expected_records",
        "downloaded_records",
        "active_workers",
        "source",
        "current_code",
        "rate_limit_cooldown_seconds",
        "last_failure_reason",
        "elapsed_seconds",
        "checkpoint_database_pattern",
        "partition_database_pattern",
        "catalog_database",
        "manifest_path",
        "checkpoint_loading",
        "supplier_query_failed_blacklisted",
        "shards/<board>-<code-prefix>.sqlite3",
        "catalog.sqlite3",
    ):
        assert required in design
    assert "运行进度字段以 `02_工程设计.md` 第 14.1 节为唯一规范" in section


def test_baostock_history_is_partitioned_by_board_and_four_digit_code_prefix() -> None:
    work = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    section = _baostock_section(work)

    for required in (
        "data/history/baostock-daily/sessions-2000/",
        "板块与股票代码前四位",
        "每个分库最多 100 只股票",
        "单个分库损坏",
        "只重新下载该分库",
    ):
        assert required in section
