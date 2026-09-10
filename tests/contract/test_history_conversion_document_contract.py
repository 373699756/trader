from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_one_time_history_conversion_is_documented_without_unblocking_daily_maintenance() -> None:
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")
    plan = (ROOT / "docs/03_工程实施.md").read_text(encoding="utf-8")
    replay = (ROOT / "docs/04_策略回溯.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for content in (design, replay, readme):
        assert "scripts/convert_baostock_history.py" in content
        assert "--offline" in content
    for token in (
        "data/history/baostock-daily/sessions-2000",
        "data/history/baostock",
        "raw/qfq/is_st",
        "单个受控 SDK 子进程",
        "资格、硬过滤和风险事实",
    ):
        assert token in design
    assert "阶段 D" in plan
    assert "history_sync_pending" in plan
    assert "不会解除 `download_history` 的阶段 D blocker" in replay


def test_converter_is_part_of_repository_quality_scope() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "scripts/convert_baostock_history.py" in makefile
