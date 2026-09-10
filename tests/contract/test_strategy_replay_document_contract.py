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

    assert "./run.sh download_history --sessions 2000" in content
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


def test_strategy_replay_document_explains_incremental_download_and_atomic_training() -> None:
    content = REPLAY.read_text(encoding="utf-8")

    for required in (
        "训练命令现已逐股读取该活动归档",
        "--mode update",
        "history-plan",
        "父归档",
        "增量 SQLite 分片",
        "active manifest",
        "parent_manifest_hash",
        "increment_manifest_hash",
        "active_data_hash",
        "动态 `source_cutoff`",
        "effective_at",
        "published_at",
        "同一 key 内容冲突",
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

    assert "当前可以执行 `--mode update`" not in content
    assert "当前快照回填历史" in content
