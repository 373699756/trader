from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPLAY = ROOT / "docs/04_策略回溯.md"


def test_strategy_replay_document_covers_the_complete_offline_to_live_chain() -> None:
    content = REPLAY.read_text(encoding="utf-8")
    ordered_sections = (
        "## 1. 文档边界与结论",
        "## 2. 一张图看懂完整链条",
        "## 3. 历史数据下载：下载什么、为什么需要",
        "## 4. 从归档到可训练样本",
        "## 5. Tomorrow 如何训练与验证",
        "## 6. 训练结果如何参与实时荐股",
        "## 7. Today、Tomorrow、D25 的关系",
        "## 8. 影响最终荐股的因素",
        "## 9. 失败、降级与不会发生的行为",
        "## 10. 操作顺序与结果判读",
    )

    positions = tuple(content.index(section) for section in ordered_sections)
    assert positions == tuple(sorted(positions))
    assert len(content.splitlines()) <= 700


def test_strategy_replay_document_explains_downloaded_data_and_uses() -> None:
    content = REPLAY.read_text(encoding="utf-8")

    for required in (
        "交易日历",
        "证券身份与上市/退市日期",
        "未复权 OHLCV",
        "前复权 OHLCV",
        "preclose",
        "pct_change",
        "turnover",
        "逐日 ST",
        "历史行业生效区间",
        "成交额",
        "停牌状态",
        "coverage",
        "content hash",
    ):
        assert required in content


def test_strategy_replay_document_explains_training_and_live_scoring() -> None:
    content = REPLAY.read_text(encoding="utf-8")

    for required in (
        "qfq_return_1d",
        "qfq_return_3d",
        "qfq_return_5d",
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
        "20bp、50bp、100bp",
        "Ridge",
        "LightGBM",
        "训练、早停、校准、确认和留出",
        "61 根历史日线",
        "预测横截面分位",
        "预测超额收益 - 估算交易成本",
        "local_score * 0.68",
        "+ deepseek_score * 0.32",
        "- deepseek_risk_penalty",
        "Top6",
        "14:50",
        "production_authority=false",
        "automatic_model_update=false",
    ):
        assert required in content


def test_strategy_replay_document_keeps_public_commands_and_strategy_ownership_bounded() -> None:
    content = REPLAY.read_text(encoding="utf-8")
    public_guides = (
        ROOT / "README.md",
        ROOT / "docs/01_评分逻辑.md",
        ROOT / "docs/02_工程设计.md",
        ROOT / "docs/03_工程实施.md",
        REPLAY,
    )

    assert "./run.sh download_history" in content
    assert "./run.sh download_history --sessions" not in content
    assert "./run.sh train-tomorrow" in content
    for guide in public_guides:
        assert "./run.sh research-status" not in guide.read_text(encoding="utf-8")
    for required in (
        "D25 的目标是筛选未来第 2 至第 5 个交易日区间内具备上涨能力的股票",
        "D25 当前是独立规则评分",
        "不读取 Tomorrow 模型",
        "Today 当前是独立规则评分",
        "Long 不评分",
    ):
        assert required in content


def test_strategy_replay_document_records_the_stage_a_b_gates_and_atomic_training() -> None:
    content = REPLAY.read_text(encoding="utf-8")

    for required in (
        "history_maintenance_status",
        "阶段 D（已完成）",
        "history-daily-capability",
        "Tushare 120 积分",
        "raw 3/3",
        "qfq/复权因子不可用",
        "腾讯 raw/qfq 3/3",
        "东财 0/3",
        "BaoStock",
        "10,906",
        "21,810 秒",
        "高效日更来源保持阻塞",
        "effective_at",
        "published_at",
        "扣成本前超额收益",
        "raw `next_return`",
        "active-bundle.json",
        "失败时保留上一活动组",
        "historical_data_insufficient",
        "point_in_time_parity=false",
        "production_authority=false",
        "默认 V1 不变",
    ):
        assert required in content

    assert "download_history --mode update" not in content
    assert "当前快照回填历史" in content


def test_strategy_replay_document_records_the_zero_argument_history_rebuild_plan() -> None:
    content = REPLAY.read_text(encoding="utf-8")
    plan = content.split("## 12. 零参数历史归档重构计划（实施中）", maxsplit=1)[1]

    for required in (
        "`./run.sh download_history` 是唯一历史维护入口",
        "不接受 `--runtime-dir`、`--sessions`、`--mode` 或 `--profile`",
        "control.sqlite3",
        "按自然年目录、自然月分片",
        "YYYY/MM.sqlite3",
        "只打开命中的月库",
        "下载顺序不决定物理布局",
        "PRIMARY KEY (trade_date, code, revision_id) WITHOUT ROWID",
        "训练样本缓存同样按 `(trade_date, code)`",
        "`(code, trade_date)`",
        "`(trade_date, board, code)`",
        "滚动 2000 个完整交易日",
        "750、1000、1250",
        "约 250 日确认区",
        "约 250 日终端留出",
        "全市场交易日历是唯一日期主轴",
        "上市前日期不造空行",
        "至少 61 个实际有效交易日",
        "同一交易日的全部股票必须进入同一切分",
        "退市股票在退市生效日前的历史继续保留",
        "每新增 20 个标签成熟交易日",
        "最近 5 个交易日",
        "新上市证券从上市日",
        "退市证券保留既有历史",
        "training_due",
        "already_current",
        "不会自动启动训练",
        "失败时继续使用上一活动模型",
        "Regression-Key: zero-argument-history-snapshot-training-alignment",
    ):
        assert required in plan

    assert "旧目录、旧活动指针、legacy、迁移、subset workaround 和对应测试均已删除" in plan
    assert "拒绝 `单个巨型 SQLite`" in plan


def test_strategy_replay_document_has_an_executable_maintenance_and_reminder_plan() -> None:
    content = REPLAY.read_text(encoding="utf-8")
    plan = content.split("## 12. 零参数历史归档重构计划（实施中）", maxsplit=1)[1]

    for required in (
        "每天触发不等于每天全市场逐股重拉",
        "5453 × 2 秒约为 3 小时",
        "日更来源能力门",
        "Asia/Shanghai 15:10",
        "Asia/Shanghai 20:30",
        "已完整发布则返回 `already_current`",
        "失败、未执行、未完整发布或中断则只补缺口",
        "Persistent=true",
        "StartWhenAvailable",
        "already_running",
        "活动模型成功训练所绑定的 `label_cutoff`",
        "matured_label_days_since_training",
        "initial_training_required",
        "cadence_due",
        "input_revision_due",
        "data_incomplete",
        "只有成功发布新训练 bundle 才能清零",
        "每天最多提醒一次",
        "automatic_model_update=false",
        "每日自动同步、到期只提醒",
        "不得自动调用 `train-tomorrow`",
        "磁盘余量",
        "控制库损坏",
        "SIGTERM",
        "阶段 A（已完成）",
        "阶段 B（已完成）",
        "SQLiteHistoryControlRepository",
        "HistoryMaintenanceLock",
        "HistoryDiskRequirement",
        "阶段 C（已完成）",
        "SQLiteHistoryMonthPartitionRepository",
        "SQLiteHistoryMonthlyArchive",
        "SQLiteHistoryTrainingCache",
        "阶段 E（已完成）",
        "阶段 F（已完成）",
        "阶段 G",
    ):
        assert required in plan
