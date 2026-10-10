#!/usr/bin/env python3
"""Keep refactor naming and complexity debt from changing without review."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "trader"
NAMING_ROOTS = (SOURCE_ROOT, PROJECT_ROOT / "scripts", PROJECT_ROOT / "tests")
SELECTED_RULES = ("C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915", "N")
# Anchored to pushed d8866ea7; equal totals cannot move debt to another owner.
EXPECTED_DEBT = frozenset(
    {
        ("PLR0913", "download/application/download_history.py", "download_history"),
        ("C901", "download/infra/baostock_gateway.py", "_reconstruct_unavailable_qfq"),
        ("C901", "http_api/handlers/product_handler.py", "_market_data"),
        ("C901", "infra/market_data/observations.py", "SourceObservation.__post_init__"),
        ("PLR0913", "recommendation/application/pipeline/data_source/source_quality.py", "build_source_stage_output"),
        (
            "PLR0913",
            "recommendation/application/pipeline/final_selection/decision_projection.py",
            "build_scored_hybrid",
        ),
        (
            "PLR0913",
            "recommendation/application/pipeline/freeze_publish/freeze_coordinator.py",
            "ScoredFreezeCoordinator.__init__",
        ),
        (
            "PLR0913",
            "recommendation/application/pipeline/freeze_publish/publication_io.py",
            "PublicationIoTracker.finish",
        ),
        (
            "PLR0913",
            "recommendation/application/pipeline/freeze_publish/runtime_adapters.py",
            "DeepSeekAdapter.__init__",
        ),
        ("PLR0913", "recommendation/application/pipeline/quality_check/pipeline_status.py", "build_supply_status"),
        ("PLR0913", "recommendation/application/pipeline/stage_output.py", "stage_output"),
        ("PLR0913", "recommendation/application/pipeline/static_market/static_market_loader.py", "load_static_market"),
        ("C901", "recommendation/domain/evidence/pipeline.py", "PipelineStageStatus.__post_init__"),
        ("C901", "recommendation/domain/market/refresh.py", "ResearchRefreshResult.__post_init__"),
        ("PLR0913", "recommendation/infra/market_data/history_recovery.py", "HistoryRecovery.__init__"),
        ("C901", "recommendation/infra/market_data/history_recovery.py", "HistoryRecovery.recover"),
        ("PLR0912", "recommendation/infra/market_data/history_recovery.py", "HistoryRecovery.recover"),
        ("PLR0915", "recommendation/infra/market_data/history_recovery.py", "HistoryRecovery.recover"),
        ("PLR0913", "recommendation/infra/market_data/published_history_cache.py", "PublishedHistoryCache.__init__"),
    }
)
RUFF_TIMEOUT_SECONDS = 60
DECLARATION_HEADER = "| Tool | Owner | Network | Writes | Output | Resource boundary |"
TOP_LEVEL_SCRIPT_MANIFEST = frozenset(
    {
        "check_refactor_quality.py",
        "check_tomorrow_training_memory.py",
        "convert_baostock_history.py",
        "diagnose_runtime.py",
        "generate_long_watchlist_asset.py",
        "migrate_runtime_data.py",
        "rename_qfq_shards.py",
        "repack_baostock_history_archive.py",
        "verify_wheel_install.py",
    }
)


def _check_inventory() -> None:
    actual_scripts = frozenset(path.name for path in (PROJECT_ROOT / "scripts").glob("*.py"))
    if actual_scripts != TOP_LEVEL_SCRIPT_MANIFEST:
        raise ValueError("top-level script inventory changed; review the owner and manifest")
    readme = (PROJECT_ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    section = readme.partition("## Retained tools\n")[2].split("\n## ", 1)[0]
    rows = [line.strip() for line in section.splitlines() if line.startswith("|")]
    if len(rows) < 2 or rows[0] != DECLARATION_HEADER or rows[1] != "| --- | --- | --- | --- | --- | --- |":
        raise ValueError("scripts/README.md must declare owner, network, writes, output and resource boundary")
    names: list[str] = []
    for row in rows[2:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) != 6 or any(not cell or cell.upper() in {"TODO", "TBD", "UNKNOWN"} for cell in cells):
            raise ValueError("retained tool declarations must contain six explicit fields")
        names.append(cells[0].strip("`"))
    if Counter(names) != Counter(TOP_LEVEL_SCRIPT_MANIFEST):
        raise ValueError("retained tool declarations must cover each top-level script exactly once")


def _function_symbols(source: Path) -> dict[int, str]:
    symbols: dict[int, str] = {}

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols[node.lineno] = ".".join(scope)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source.read_text(encoding="utf-8")), ())
    return symbols


def _read_debt(stdout: str) -> Counter[tuple[str, str, str]]:
    diagnostics = json.loads(stdout)
    if not isinstance(diagnostics, list):
        raise ValueError("Ruff diagnostics root must be a list")
    debt: Counter[tuple[str, str, str]] = Counter()
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            raise ValueError("Ruff diagnostic must be an object")
        code, filename, location = diagnostic.get("code"), diagnostic.get("filename"), diagnostic.get("location")
        if not isinstance(code, str) or not isinstance(filename, str) or not isinstance(location, dict):
            raise ValueError("Ruff diagnostic is missing its rule, filename or location")
        row = location.get("row")
        if type(row) is not int or row <= 0:
            raise ValueError("Ruff diagnostic has an invalid row")
        source = Path(filename).resolve()
        relative = source.relative_to(SOURCE_ROOT).as_posix()
        symbol = _function_symbols(source).get(row)
        if symbol is None:
            raise ValueError("strict diagnostic must identify a function definition")
        debt[(code, relative, symbol)] += 1
    return debt


def _run_ruff(
    roots: tuple[Path, ...], rules: tuple[str, ...], *, json_output: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "ruff",
            "check",
            *(str(path) for path in roots),
            "--no-cache",
            "--select",
            ",".join(rules),
            *(("--output-format", "json") if json_output else ()),
        ),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=RUFF_TIMEOUT_SECONDS,
    )


def main() -> int:
    try:
        _check_inventory()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("cannot read retained tool declarations", file=sys.stderr)
        return 2
    try:
        result = _run_ruff((SOURCE_ROOT,), SELECTED_RULES, json_output=True)
        if result.returncode not in {0, 1}:
            print("strict Ruff invocation failed", file=sys.stderr)
            return 2
        actual = _read_debt(result.stdout)
        if (result.returncode == 0) != (not actual):
            raise ValueError("Ruff status disagrees with its diagnostics")
        expected = Counter(EXPECTED_DEBT)
        if actual != expected:
            print("strict refactor debt changed; review owners before updating EXPECTED_DEBT", file=sys.stderr)
            print(f"removed: {sorted((expected - actual).elements())}", file=sys.stderr)
            print(f"added:   {sorted((actual - expected).elements())}", file=sys.stderr)
            return 1
        naming = _run_ruff(NAMING_ROOTS, ("N",))
    except (OSError, UnicodeError, ValueError, SyntaxError):
        print("cannot execute Ruff or resolve strict diagnostics", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("Ruff quality check exceeded its 60 second deadline", file=sys.stderr)
        return 2
    if naming.returncode != 0:
        sys.stderr.write(naming.stdout)
        sys.stderr.write(naming.stderr)
        return 1 if naming.returncode == 1 else 2

    print("Retained tool declarations, strict debt owners and repository-wide naming rules verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
