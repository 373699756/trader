from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "trader"
RECOMMENDATION = SOURCE / "recommendation" / "application"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_recommendation_capabilities_have_explicit_application_boundaries() -> None:
    expected = {
        "ports",
        "pipeline",
    }
    assert expected <= {path.name for path in RECOMMENDATION.iterdir()}

    forbidden = ("trader.infra", "trader.web", "trader.entrypoints", "stock_analyzer")
    violations = [
        f"{path.relative_to(SOURCE)} -> {imported}"
        for path in RECOMMENDATION.rglob("*.py")
        for imported in _imports(path)
        if imported.startswith(forbidden) or ".training" in imported or imported.endswith(".download")
    ]
    assert violations == []


def test_recommendation_runtime_uses_ports_instead_of_training_entrypoints() -> None:
    input_runtime = (SOURCE / "recommendation/application/pipeline/data_source/source_router.py").read_text(
        encoding="utf-8"
    )
    decision_adapters = (SOURCE / "application" / "decisions" / "decision_adapters.py").read_text(encoding="utf-8")

    assert "CandidateFilteringPort" in input_runtime
    assert "LocalScoringPort" in input_runtime
    assert "ScoreFusionPort" in decision_adapters
    assert "build_candidate_plans" not in input_runtime
    assert "refresh_candidate_reserves" not in input_runtime
    assert "build_scored_local" not in input_runtime
    assert "build_scored_hybrid" not in decision_adapters
    assert "run_v2_training" not in input_runtime
    assert "run_v3_training" not in input_runtime


def test_composition_root_explicitly_assembles_recommendation_capabilities() -> None:
    bootstrap = (SOURCE / "bootstrap.py").read_text(encoding="utf-8")

    for capability in (
        "CandidateFilteringService(",
        "LocalScoringService(",
        "PublishedModelScoringService(",
        "RankingSelectionService()",
        "RiskControlService()",
        "ScoreFusionService()",
    ):
        assert capability in bootstrap


def test_infrastructure_technical_capabilities_have_single_owners() -> None:
    atomic = SOURCE / "infra" / "atomic_files" / "json.py"
    clock = SOURCE / "infra" / "clock" / "shanghai.py"
    assert atomic.is_file()
    assert clock.is_file()
    assert not (SOURCE / "infra" / "persistence" / "runtime_json.py").exists()
    assert "class RuntimeJsonWriter" in atomic.read_text(encoding="utf-8")
    assert "class ShanghaiClock" in clock.read_text(encoding="utf-8")

    definitions: dict[str, list[Path]] = {
        "RuntimeJsonWriter": [],
        "atomic_read_json": [],
        "atomic_write_json": [],
        "ShanghaiClock": [],
    }
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in definitions:
                definitions[node.name].append(path.relative_to(SOURCE))

    assert definitions == {
        "RuntimeJsonWriter": [Path("infra/atomic_files/json.py")],
        "atomic_read_json": [Path("infra/atomic_files/json.py")],
        "atomic_write_json": [Path("infra/atomic_files/json.py")],
        "ShanghaiClock": [Path("infra/clock/shanghai.py")],
    }


def test_architecture_contract_documents_the_three_boundaries() -> None:
    design = (ROOT / "docs" / "02_工程设计.md").read_text(encoding="utf-8")
    for name in (
        "candidate_filtering.py",
        "feature_calculation.py",
        "local_scoring.py",
        "model_scoring.py",
        "risk_control.py",
        "ranking_selection.py",
        "score_fusion.py",
        "infra/atomic_files",
        "infra/clock",
    ):
        assert name in design
    assert "运行链与训练链保持单向隔离" in design
