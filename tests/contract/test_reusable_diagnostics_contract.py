from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIFIED_DIAGNOSTIC = ROOT / "scripts" / "diagnose_runtime.py"
REMOVED_DIAGNOSTIC_WRAPPERS = (
    ROOT / "scripts" / "check_web_recommendation_health.py",
    ROOT / "scripts" / "measure_web_refresh_interval.py",
    ROOT / "scripts" / "sample_history_sources.py",
    ROOT / "scripts" / "sample_tushare_daily.py",
    ROOT / "scripts" / "sample_tencent_quotes.py",
    ROOT / "scripts" / "run_production_performance.py",
)
INTERNAL_DIAGNOSTIC_MODULES = (
    ROOT / "scripts" / "runtime_diagnostics" / "web_health.py",
    ROOT / "scripts" / "runtime_diagnostics" / "browser_refresh.py",
    ROOT / "scripts" / "runtime_diagnostics" / "history_sources.py",
    ROOT / "scripts" / "runtime_diagnostics" / "tencent_quotes.py",
    ROOT / "scripts" / "runtime_diagnostics" / "tushare_daily.py",
)
INTERNAL_COMMON = ROOT / "scripts" / "runtime_diagnostics" / "common.py"


def test_unified_runtime_diagnostic_is_the_only_public_parameterized_script() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    source = UNIFIED_DIAGNOSTIC.read_text(encoding="utf-8")

    assert "def main() -> int:" in source
    assert "argparse.ArgumentParser" in source
    assert "tests." not in source
    result = subprocess.run(
        (sys.executable, str(UNIFIED_DIAGNOSTIC), "--help"),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    for option in ("--profile", "--base-url", "--runtime-config", "--codes", "--output"):
        assert option in result.stdout
    for profile in ("web", "history", "tencent", "tushare", "browser", "performance", "live", "full"):
        assert profile in result.stdout
    assert UNIFIED_DIAGNOSTIC.name in makefile


def test_legacy_diagnostic_wrappers_are_deleted_after_unified_cli_migration() -> None:
    active_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "Makefile",
            ROOT / "README.md",
            ROOT / "docs" / "software-business-design.md",
            ROOT / "src" / "trader" / "entrypoints" / "performance.py",
            ROOT / ".agents" / "skills" / "trader-delivery" / "SKILL.md",
            ROOT / ".agents" / "skills" / "trader-delivery" / "references" / "runtime-diagnostics.md",
        )
    )
    for wrapper in REMOVED_DIAGNOSTIC_WRAPPERS:
        assert not wrapper.exists()
        assert wrapper.name not in active_text

    for module in INTERNAL_DIAGNOSTIC_MODULES:
        source = module.read_text(encoding="utf-8")
        assert "def main() -> int:" in source
        assert "argparse.ArgumentParser" in source
        assert "from .common import emit_report" in source
        result = subprocess.run(
            (sys.executable, "-m", f"scripts.runtime_diagnostics.{module.stem}", "--help"),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "--output" not in result.stdout

    common = INTERNAL_COMMON.read_text(encoding="utf-8")
    assert "def emit_report(" in common


def test_agent_workflow_requires_reusing_diagnostic_scripts() -> None:
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "可复用的诊断、性能、数据源或浏览器实测" in instructions
    assert "固化到 `scripts/`" in instructions
    assert "优先复用或扩展已有脚本" in instructions
    assert "不得在 `/tmp`" in instructions


def test_repository_delivery_skill_is_discoverable_and_routes_diagnostics() -> None:
    skill_root = ROOT / ".agents" / "skills" / "trader-delivery"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    metadata = (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "name: trader-delivery" in skill
    assert "scripts/diagnose_runtime.py" in skill
    assert "references/change-impact-matrix.md" in skill
    assert "references/runtime-diagnostics.md" in skill
    assert "references/delivery-evidence.md" in skill
    assert "$trader-delivery" in metadata
    assert "allow_implicit_invocation: true" in metadata
    assert "!.agents/skills/trader-delivery/" in ignore_rules
    assert "必须加载仓库级 `$trader-delivery` skill" in instructions
    assert "--persistence-runtime-dir" in (ROOT / "README.md").read_text(encoding="utf-8")

    for reference in ("change-impact-matrix.md", "runtime-diagnostics.md", "delivery-evidence.md"):
        assert (skill_root / "references" / reference).is_file()
