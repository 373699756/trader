from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
REPORT = ROOT / "docs" / "reports" / "v2-p1-source-capability-baseline.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
GATEWAY = ROOT / "src" / "trader" / "infra" / "market_data" / "gateway.py"
COORDINATOR = ROOT / "src" / "trader" / "infra" / "market_data" / "source_coordinator.py"
SETTINGS_RUNTIME = ROOT / "src" / "trader" / "infra" / "settings_runtime.py"
MARKET_DIR = ROOT / "src" / "trader" / "infra" / "market_data"


def test_v2_plan_keeps_source_admission_in_data_plane_scope() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    report_token = "v2-p1-source-capability-baseline.md"
    assert "### V2-E1：统一 V2 数据平面" in plan
    assert "`SourceCapability` 清单" in plan
    assert "未验证来源不进入评分、冻结、组合根或生产配置" in plan
    assert report_token in plan
    assert REPORT.exists()


def test_v2_p1_source_capability_report_covers_required_sources() -> None:
    report = REPORT.read_text(encoding="utf-8")

    for source in (
        "交易所官方",
        "巨潮资讯 CNInfo",
        "东方财富",
        "新浪",
        "腾讯",
        "通达信/mootdx",
        "BaoStock",
        "AKShare",
        "Tushare",
    ):
        assert source in report

    assert "本批不引入新的网络抓取 fixture" in report
    assert "SourceCapability 清单" in report


def test_unimplemented_sources_are_not_wired_into_market_runtime() -> None:
    for missing in ("exchange.py", "baostock.py", "mootdx.py"):
        assert not (MARKET_DIR / missing).exists()
    assert (MARKET_DIR / "cninfo.py").exists()

    for path in (BOOTSTRAP, GATEWAY, COORDINATOR):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        for forbidden in ("exchange", "cninfo", "baostock", "mootdx"):
            assert all(forbidden not in name.split(".") for name in imports), f"unexpected {forbidden} in {path}"


def test_source_contract_versions_contract_expected_five_sources() -> None:
    text = SETTINGS_RUNTIME.read_text(encoding="utf-8")
    expected_raw = re.search(r"expected = \{([^}]*)\}", text)
    assert expected_raw is not None, "market_data.source_contract_versions expected set not found"
    actual = {
        token.strip().strip("\"'").split(":")[0]
        for token in expected_raw.group(1).replace("\n", " ").split(",")
        if token.strip()
    }
    assert actual == {"eastmoney", "sina", "tencent", "tushare", "akshare"}
