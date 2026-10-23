from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.module_boundaries import SourceImports, SourceImportTargets

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "trader"


def test_recommendation_runtime_uses_ports_instead_of_training_entrypoints() -> None:
    input_runtime = (SOURCE / "recommendation/application/pipeline/data_source/source_router.py").read_text(
        encoding="utf-8"
    )
    decision_adapters = (SOURCE / "recommendation/application/pipeline/freeze_publish/runtime_adapters.py").read_text(
        encoding="utf-8"
    )
    local_builder = (SOURCE / "recommendation/application/pipeline/local_score/local_decision_builder.py").read_text(
        encoding="utf-8"
    )

    assert "CandidateFilteringPort" in input_runtime
    assert "LocalScoringPort" not in input_runtime
    assert "LocalScoringPort" in local_builder
    assert "ScoreFusionPort" in decision_adapters
    assert "build_candidate_plans" not in input_runtime
    assert "refresh_candidate_reserves" not in input_runtime
    assert "build_scored_local" not in input_runtime
    assert "build_scored_hybrid" not in decision_adapters
    assert "run_v2_training" not in input_runtime
    assert "run_v3_training" not in input_runtime
    assert "run_v2_training" not in local_builder
    assert "run_v3_training" not in local_builder


def test_composition_root_explicitly_imports_recommendation_capabilities() -> None:
    bootstrap = SOURCE / "bootstrap.py"
    imported = {item.member for item in SourceImports(bootstrap, SOURCE)}
    for capability in (
        "CandidateFilteringService",
        "LocalScoringService",
        "RankingSelectionService",
        "RiskControlService",
        "ScoreFusionService",
    ):
        assert capability in imported

    factory = SOURCE / "recommendation/infra/scoring/profile_factory.py"
    assert "trader.recommendation.infra.scoring.profile_factory" in SourceImportTargets(bootstrap, SOURCE)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "profile_factory"
        and node.func.attr == "BuildProfileScoring"
        for node in ast.walk(ast.parse(bootstrap.read_text(encoding="utf-8")))
    )
    assert "PublishedModelScoringService" in {item.member for item in SourceImports(factory, SOURCE)}


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
        "AtomicReadJson": [],
        "AtomicWriteJson": [],
        "ShanghaiClock": [],
    }
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in definitions:
                definitions[node.name].append(path.relative_to(SOURCE))

    assert definitions == {
        "RuntimeJsonWriter": [Path("infra/atomic_files/json.py")],
        "AtomicReadJson": [Path("infra/atomic_files/json.py")],
        "AtomicWriteJson": [Path("infra/atomic_files/json.py")],
        "ShanghaiClock": [Path("infra/clock/shanghai.py")],
    }


def test_recommendation_pipeline_capabilities_have_active_owners() -> None:
    pipeline = SOURCE / "recommendation/application/pipeline"
    for name in (
        "static_filter",
        "dynamic_filter",
        "candidate_pool",
        "quality_check",
        "local_score",
        "risk_review",
        "score_merge",
        "final_selection",
    ):
        assert any(path.name != "__init__.py" for path in (pipeline / name).rglob("*.py")), name
