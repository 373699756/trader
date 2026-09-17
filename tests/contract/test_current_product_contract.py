from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_authoritative_design_requires_one_final_product_chain_without_runtime_compatibility() -> None:
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")

    for statement in (
        "current-only 最终 release 边界",
        "新 release 不读取旧运行目录、旧数据库、旧快照或旧 schema",
        "当前唯一运行目录固定为 `.runtime/trader`",
        "旧 release 只能与其对应旧运行目录整体回退",
        "不得提供旧 API 别名、重定向、弃用窗口、双读或双写",
        "GET /api/decisions/<strategy>/current",
        "GET /api/decisions/<strategy>/history?date=YYYY-MM-DD",
        "GET /api/decisions/<strategy>/dates",
        "GET /api/status",
        "GET /api/events",
    ):
        assert statement in design
    assert "当前交付状态：current-only 工程与发布门禁验收已闭合" not in design


def test_strategy_contract_focuses_on_current_decisions_without_release_chronology() -> None:
    strategy = (ROOT / "docs/01_评分逻辑.md").read_text(encoding="utf-8")
    compact = " ".join(strategy.split())

    for statement in (
        "最终 V2 评分口径只产生 `ScoredDecision`",
        "当前链只产生 `ScoredDecision` 和 `LongProjection`",
        "不构造 `RecommendationSnapshot`",
        "旧策略、旧引擎或旧 schema 不参与当前链重新评分",
        "long 不借用评分字段伪造荐股决策形状",
    ):
        assert statement in compact
    assert "正式 0.2.0 release 尚未声明" not in compact


def test_release_guides_expose_only_unified_api_and_desktop_gate() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")

    for content in (readme, operations):
        assert "/api/status" in content
        assert "/api/v2/" not in content
        assert "trader-cli perf-check" not in content
    assert "deepseek-budget.sqlite3" in operations
    assert "卡脖子、高成长、低价潜力" in operations
    for public_command in (
        "./run.sh check",
        "./run.sh download",
        "./run.sh train-v2",
        "./run.sh train-v3",
    ):
        assert public_command in readme
        assert public_command in operations
    for retired_command in ("./run.sh validate-config", "./run.sh performance-check"):
        assert retired_command not in readme
        assert retired_command not in operations
    assert "curl -fsS http://127.0.0.1:5000/api/status" in operations
    assert (ROOT / "tests/performance/run_desktop_dashboard.py").is_file()
    assert not (ROOT / "tests/performance/run_chrome_dashboard.py").exists()
