from __future__ import annotations

import re
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
    ROOT / "scripts" / "runtime_diagnostics" / "exchange_security_master.py",
    ROOT / "scripts" / "runtime_diagnostics" / "tencent_quotes.py",
    ROOT / "scripts" / "runtime_diagnostics" / "tushare_daily.py",
    ROOT / "scripts" / "runtime_diagnostics" / "history_archive_performance.py",
)
INTERNAL_REPORTING = ROOT / "scripts" / "runtime_diagnostics" / "reporting.py"
SKILL_ROOT = ROOT / ".agents" / "skills" / "trader-delivery"


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
    for option in (
        "--profile",
        "--base-url",
        "--runtime-config",
        "--codes",
        "--history-source",
        "--output",
        "--archive-root",
        "--archive-page-sample-count",
        "--archive-query-rounds",
        "--archive-revision-write-sample-count",
    ):
        assert option in result.stdout
    for profile in (
        "web",
        "history",
        "security-master",
        "tencent",
        "tushare",
        "browser",
        "performance",
        "history-archive",
        "live",
        "full",
    ):
        assert profile in result.stdout
    assert UNIFIED_DIAGNOSTIC.name in makefile


def test_legacy_diagnostic_wrappers_are_deleted_after_unified_cli_migration() -> None:
    active_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "Makefile",
            ROOT / "README.md",
            ROOT / "docs" / "02_工程设计.md",
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
        assert "from .reporting import emit_report" in source
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

    reporting = INTERNAL_REPORTING.read_text(encoding="utf-8")
    assert "def emit_report(" in reporting


def test_repository_delivery_skill_links_resolve_and_route_current_contracts() -> None:
    skill_files = (
        SKILL_ROOT / "SKILL.md",
        *(SKILL_ROOT / "references").glob("*.md"),
    )
    active_text = "\n".join(path.read_text(encoding="utf-8") for path in skill_files)

    for source in skill_files:
        content = source.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+\.md)(?:#[^)]+)?\)", content):
            assert (source.parent / target).is_file(), f"missing skill reference: {source.name} -> {target}"

    assert "docs/01_评分逻辑.md" in active_text
    assert "docs/02_工程设计.md" in active_text
    assert "docs/changelog/" in active_text
    assert "src/trader/recommendation/application/runtime/" in active_text
    assert "tests/integration/test_scheduler_runtime.py" in active_text
    assert "/api/status" in active_text
    assert "config/runtime.json" in active_text
    assert "trader-runtime-diagnostics" in active_text

    for current_target in (
        ROOT / "docs" / "01_评分逻辑.md",
        ROOT / "docs" / "02_工程设计.md",
        ROOT / "docs" / "changelog",
        ROOT / "src" / "trader" / "recommendation" / "application" / "runtime",
        ROOT / "tests" / "integration" / "test_scheduler_runtime.py",
        ROOT / "config" / "runtime.json",
    ):
        assert current_target.exists(), f"skill route does not exist: {current_target.relative_to(ROOT)}"

    for retired_reference in (
        "docs/software-" + "business-design.md",
        "docs/recommendation-" + "strategy.md",
        "src/trader/application/{" + "v2_runtime",
        "test_" + "v2_scheduler_runtime.py",
        "/api/" + "v2/status",
        "config/" + "v2/runtime.json",
        "trader-runtime-diagnostics-" + "v1",
        "V2" + "RefreshOutcome",
    ):
        assert retired_reference not in active_text


def test_delivery_skill_preserves_recommendation_funnel_incident_checkpoints() -> None:
    playbook = (
        ROOT / ".agents" / "skills" / "trader-delivery" / "references" / "recommendation-funnel-incidents.md"
    ).read_text(encoding="utf-8")

    for checkpoint in (
        "host-network-reachability",
        "stage-by-stage-refresh",
        "timezone-normalization",
        "funnel-semantic-classification",
        "freeze-window-control",
        "current-release-restart",
    ):
        assert checkpoint in playbook
    for incident_term in (
        "connection_failed",
        "refresh:value_error",
        "RefreshOutcome",
        "Asia/Shanghai",
        "candidate_quotes_pending",
        "security_master_coverage_incomplete",
        "close_quotes",
    ):
        assert incident_term in playbook
