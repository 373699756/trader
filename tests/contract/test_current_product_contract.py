from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_authoritative_design_requires_one_final_product_chain_without_runtime_compatibility() -> None:
    design = (ROOT / "docs/2_工程设计.md").read_text(encoding="utf-8")

    for statement in (
        "活动产品代码只在src/trader，组合根为bootstrap.py",
        "不保留旧策略页面或带项目版本前缀的兼容路由",
        "runtime_dir承载锁、日志和可丢弃临时产物",
        "数据回退必须完整release配合匹配数据副本",
        "缺数据或模型时只读Web仍可启动并报告未就绪",
        "GET /api/freezes/<strategy>/current",
        "GET /api/freezes/<strategy>/history?date=YYYY-MM-DD",
        "GET /api/freezes/<strategy>/dates",
        "GET /api/status",
        "GET /api/events",
    ):
        assert statement in design
    assert "当前交付状态：current-only 工程与发布门禁验收已闭合" not in design


def test_strategy_contract_distinguishes_current_profile_authority_from_return_validation() -> None:
    strategy = (ROOT / "docs/1_评分逻辑.md").read_text(encoding="utf-8")
    compact = " ".join(strategy.split())

    for statement in (
        "全新V1已获用户明确授权正式接入",
        "V1使用第6.4节的独立模型排序分",
        "V5保持工程Shadow",
        "Long：不冻结、不写推荐历史",
        "不能宣称已证明持续盈利",
    ):
        assert statement in compact
    assert "正式 0.2.0 release 尚未声明" not in compact


def test_release_guides_expose_only_unified_api_and_desktop_gate() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs/2_工程设计.md").read_text(encoding="utf-8")

    for content in (readme, operations):
        assert "/api/v2/" not in content
        assert "trader-cli perf-check" not in content
        assert "scripts/diagnose_runtime.py" in content
    assert "GET /api/status" in operations
    assert "chokepoint/future_growth/low_price_potential" in operations
    storage = (ROOT / "docs/4_下载训练.md").read_text(encoding="utf-8")
    assert "deepseek_budget.sqlite3" in storage
    for public_command in (
        "./run.sh sync",
        "./run.sh train",
        "./run.sh sync market",
        "./run.sh sync company",
    ):
        assert public_command in readme
        assert public_command in storage
    for retired_command in ("./run.sh validate-config", "./run.sh performance-check"):
        assert retired_command not in readme
        assert retired_command not in operations
    assert (ROOT / "tests/performance/run_desktop_dashboard.py").is_file()
    assert not (ROOT / "tests/performance/run_chrome_dashboard.py").exists()
