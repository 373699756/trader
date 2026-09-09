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


def test_remaining_increment_plan_preserves_parent_and_partition_identity() -> None:
    work = _read("docs/03_工程实施.md")
    section = work[work.index("## 2. 当前执行章节：BaoStock") : work.index("## 3.")]

    for token in (
        "旧 92 个 SQLite 分片",
        "父 manifest 原字节不变",
        "增量分片",
        "active manifest",
        "同一 key 同内容幂等",
        "父加增量的有类型只读视图",
    ):
        assert token in section


def test_dynamic_cutoff_keeps_legacy_identity_read_only() -> None:
    work = _read("docs/03_工程实施.md")
    section = work[work.index("## 3. 动态截止日") : work.index("## 4.")]

    assert "source_cutoff" in section
    assert "历史工件/父归档解码时原样只读兼容" in section
    assert "不得自动迁移、覆盖或产生新的项目版本名" in section
    assert "禁止用当前快照回填历史" in section


def test_holdout_isolation_remains_closed_until_real_point_in_time_evidence() -> None:
    work = _read("docs/03_工程实施.md")

    assert "blocked_by_external_point_in_time_sources" in work
    assert "terminal_holdout" in work
    assert "production_authority=false" in work
    assert "用户在独立批次明确授权" in work
