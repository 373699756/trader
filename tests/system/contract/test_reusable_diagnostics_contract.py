from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.helpers.shared.python_source import source_text, source_tree

ROOT = Path(__file__).resolve().parents[3]
UNIFIED_TOOL = ROOT / "scripts" / "tools.py"
REMOVED_DIAGNOSTIC_WRAPPERS = (
    ROOT / "scripts" / "check_web_recommendation_health.py",
    ROOT / "scripts" / "measure_web_refresh_interval.py",
    ROOT / "scripts" / "sample_history_sources.py",
    ROOT / "scripts" / "sample_tushare_daily.py",
    ROOT / "scripts" / "sample_tencent_quotes.py",
    ROOT / "scripts" / "run_production_performance.py",
    ROOT / "scripts" / "diagnose_runtime.py",
    ROOT / "scripts" / "generate_long_watchlist_asset.py",
    ROOT / "scripts" / "migrate_runtime_data.py",
    ROOT / "scripts" / "runtime_migration.py",
    ROOT / "scripts" / "repack_history_partitions.py",
)
def test_unified_tool_is_the_only_public_parameterized_script() -> None:
    source = source_text(UNIFIED_TOOL)
    assert len(source.splitlines()) < 40
    result = subprocess.run((sys.executable, str(UNIFIED_TOOL), "--help"), cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    for option in ("--business", "--module", "--action", "--report", "--report-format"):
        assert option in result.stdout
    assert "--profile {" not in result.stdout
    assert "--check" not in result.stdout
    assert "--output" not in result.stdout
    assert {path.name for path in (ROOT / "scripts").glob("*.py")} - {"__init__.py"} == {"tools.py"}


def test_legacy_diagnostic_wrappers_are_deleted_after_unified_cli_migration() -> None:
    import ast
    from scripts.core.catalog import WORKERS
    for wrapper in REMOVED_DIAGNOSTIC_WRAPPERS:
        assert not wrapper.exists()
    for retired in ("checks", "runtime_diagnostics", "code_quality", "skill_evaluation"):
        assert not tuple((ROOT / "scripts" / retired).glob("*.py"))
    for name, _ in WORKERS:
        path = ROOT / (name.replace(".", "/") + ".py")
        tree = source_tree(path)
        assert any(isinstance(node, ast.FunctionDef) and node.name == "Main" for node in tree.body), name
        assert not any(isinstance(node, ast.If) and "__name__" in ast.unparse(node.test) for node in tree.body), name


def test_tool_core_only_imports_neutral_script_modules() -> None:
    import ast

    violations = []
    for path in (ROOT / "scripts/core").glob("*.py"):
        for node in ast.walk(source_tree(path)):
            modules = (
                (node.module or "",) if isinstance(node, ast.ImportFrom)
                else tuple(alias.name for alias in node.names) if isinstance(node, ast.Import) else ()
            )
            for module in modules:
                if module.startswith("trader.") or (module.startswith("scripts.") and not module.startswith("scripts.core.")):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
    assert violations == []
