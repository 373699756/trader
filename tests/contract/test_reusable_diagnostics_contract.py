from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIAGNOSTICS = (
    ROOT / "scripts" / "measure_web_refresh_interval.py",
    ROOT / "scripts" / "sample_tencent_quotes.py",
)


def test_reusable_runtime_diagnostics_are_parameterized_repository_scripts() -> None:
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


def test_agent_workflow_requires_reusing_diagnostic_scripts() -> None:
    instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "可复用的诊断、性能、数据源或浏览器实测" in instructions
    assert "固化到 `scripts/`" in instructions
    assert "优先复用或扩展已有脚本" in instructions
    assert "不得在 `/tmp`" in instructions
