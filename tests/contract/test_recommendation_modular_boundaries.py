from __future__ import annotations

import ast
from pathlib import Path

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
        "static_filter/",
        "dynamic_filter/",
        "candidate_pool/",
        "quality_check/",
        "local_score/",
        "risk_review/",
        "score_merge/",
        "final_selection/",
    ):
        assert name in design
    assert "业务间只传有类型不可变对象，不共享可变缓存" in design
