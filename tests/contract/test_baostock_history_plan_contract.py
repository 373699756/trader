from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_zero_argument_supplier_contract_lives_in_design_not_the_unfinished_plan() -> None:
    design = " ".join(_read("docs/02_工程设计.md").split())
    work = _read("docs/03_工程实施.md")

    for token in (
        "单个受控 BaoStock SDK 子进程",
        "单次供应商调用硬超时",
        "最多 2 次重试",
        "查询至少间隔 2 秒",
        "10 秒",
    ):
        assert token in design
    assert "baostock_daily_archive" not in work
    assert "`completed`" not in work


def test_retired_increment_contract_is_not_a_training_input() -> None:
    design = _read("docs/02_工程设计.md")
    work = _read("docs/03_工程实施.md")

    for token in (
        "partitions/YYYY/MM.sqlite3",
        "active snapshot",
        "最近 5 日",
        "重复内容幂等",
    ):
        assert token in design
    assert "训练只接受父归档加增量归档" not in design
    assert "baostock_increment_archive" not in work


def test_dynamic_cutoff_keeps_point_in_time_facts_fail_closed() -> None:
    design = _read("docs/02_工程设计.md")
    replay = _read("docs/04_策略回溯.md")
    work = _read("docs/03_工程实施.md")

    for required in (
        "初次建立滚动 2000 个完整交易日",
        "公司行动只",
        "无法证明 `effective_at`/`published_at` 的事实保持缺失",
        "禁止当前快照回填历史",
    ):
        assert required in design
    assert "活动 snapshot 只读视图" in replay
    assert "dynamic_cutoff_and_missing_fact_acquisition" not in work


def test_holdout_isolation_remains_closed_until_real_point_in_time_evidence() -> None:
    work = _read("docs/03_工程实施.md")

    assert "blocked_by_external_point_in_time_sources" in work
    assert "terminal_holdout" in work
    assert "production_authority=false" in work
    assert "用户在独立批次明确授权" in work
