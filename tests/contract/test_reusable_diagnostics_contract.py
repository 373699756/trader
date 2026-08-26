from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTICS = (
    ROOT / "scripts" / "diagnose_runtime.py",
    ROOT / "scripts" / "check_web_recommendation_health.py",
    ROOT / "scripts" / "measure_web_refresh_interval.py",
    ROOT / "scripts" / "sample_history_sources.py",
    ROOT / "scripts" / "sample_tushare_daily.py",
    ROOT / "scripts" / "sample_tencent_quotes.py",
    ROOT / "scripts" / "run_production_performance.py",
)


def test_reusable_runtime_diagnostics_are_parameterized_repository_scripts() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for script in DIAGNOSTICS:
        source = script.read_text(encoding="utf-8")

        assert "def main() -> int:" in source
        assert "argparse.ArgumentParser" in source
        assert "tests." not in source
        result = subprocess.run(
            (sys.executable, str(script), "--help"),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "--output" in result.stdout
        assert script.name in makefile
        if script.name == "diagnose_runtime.py":
            for option in ("--profile", "--base-url", "--runtime-config", "--codes"):
                assert option in result.stdout
        if script.name == "check_web_recommendation_health.py":
            for option in ("--base-url", "--samples", "--interval-seconds", "--strategy"):
                assert option in result.stdout
        if script.name == "measure_web_refresh_interval.py":
            assert "--runtime-config" in result.stdout
        if script.name == "sample_history_sources.py":
            for option in (
                "--codes",
                "--workers",
                "--source",
                "--timeout-seconds",
                "--persistence-runtime-dir",
            ):
                assert option in result.stdout
        if script.name == "sample_tushare_daily.py":
            for option in ("--runtime-config", "--codes", "--days"):
                assert option in result.stdout


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
