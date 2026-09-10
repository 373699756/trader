from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_completed_baostock_contract_lives_in_design_not_the_unfinished_plan() -> None:
    design = " ".join(_read("docs/02_工程设计.md").split())
    work = _read("docs/03_工程实施.md")

    for token in (
        "baostock_daily_core",
        "sessions-2000",
        "每个待封存分片执行整文件 SHA-256",
        "单次供应商调用墙钟上限 60 秒",
        "最多重试 2 次",
        "每次查询至少间隔 2 秒",
    ):
        assert token in design
    assert "baostock_daily_archive" not in work
    assert "`completed`" not in work


def test_completed_increment_contract_lives_in_design_not_the_unfinished_plan() -> None:
    design = _read("docs/02_工程设计.md")
    work = _read("docs/03_工程实施.md")

    for token in (
        "旧 92 个 SQLite 分片",
        "父 manifest 原字节不变",
        "增量分片",
        "active manifest",
        "同一 key 同内容幂等",
        "父加增量的有类型只读视图",
    ):
        assert token in design
    assert "baostock_increment_archive" not in work


def test_dynamic_cutoff_keeps_legacy_identity_read_only() -> None:
    design = _read("docs/02_工程设计.md")
    replay = _read("docs/04_策略回溯.md")
    work = _read("docs/03_工程实施.md")

    for required in (
        "动态 `source_cutoff`",
        "精确、有序、唯一的交易日元组",
        "只允许解码既有父工件",
        "不得用当前快照回填历史",
    ):
        assert required in design
    assert "冻结训练输入描述" in replay
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work


def test_holdout_isolation_remains_closed_until_real_point_in_time_evidence() -> None:
    work = _read("docs/03_工程实施.md")

    assert "blocked_by_external_point_in_time_sources" in work
    assert "terminal_holdout" in work
    assert "production_authority=false" in work
    assert "用户在独立批次明确授权" in work
