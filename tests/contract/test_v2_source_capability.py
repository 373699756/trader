from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs" / "reports" / "v2-p1-source-capability-baseline.md"
BOOTSTRAP = ROOT / "src" / "trader" / "bootstrap.py"
GATEWAY = ROOT / "src" / "trader" / "infra" / "market_data" / "gateway.py"
COORDINATOR = ROOT / "src" / "trader" / "infra" / "market_data" / "source_coordinator.py"
SETTINGS_RUNTIME = ROOT / "src" / "trader" / "infra" / "settings_runtime.py"
MARKET_DIR = ROOT / "src" / "trader" / "infra" / "market_data"
MARKET_PORTS = ROOT / "src" / "trader" / "application" / "ports" / "market.py"
DATA_PLANE_PORTS = ROOT / "src" / "trader" / "application" / "ports" / "data_plane.py"


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


def test_v2_e1_freezes_unified_read_and_persisted_calendar_ports() -> None:
    market_tree = ast.parse(MARKET_PORTS.read_text(encoding="utf-8"))
    data_plane_tree = ast.parse(DATA_PLANE_PORTS.read_text(encoding="utf-8"))
    market_classes = {node.name for node in ast.walk(market_tree) if isinstance(node, ast.ClassDef)}
    data_plane_classes = {node.name for node in ast.walk(data_plane_tree) if isinstance(node, ast.ClassDef)}

    assert "DataPlaneReadPort" in market_classes
    assert "DataPlaneCoverage" in market_classes
    assert "TradingCalendarRecord" in data_plane_classes
