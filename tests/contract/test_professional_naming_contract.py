from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "trader"

FORBIDDEN_ACTIVE_PATHS = (
    "application/ports/types.py",
    "application/research/challenger_models.py",
    "application/research/cost_aware_selection_models.py",
    "application/research/factor_diagnostic_models.py",
    "application/research/models.py",
    "application/research/replay_models.py",
    "application/research/research_tomorrow_orchestrator.py",
    "application/research/shadow_models.py",
    "application/research/tomorrow_daily_close_c3.py",
    "application/research/tomorrow_feature_models.py",
    "application/research/tomorrow_historical_models.py",
    "application/research/transparent_candidate.py",
    "application/research/shadow_model_models.py",
    "application/runtime/runtime.py",
    "domain/research/transparent_candidate.py",
    "infra/deepseek/base_client.py",
    "infra/market_data/ground_truth.py",
    "infra/market_data/history/service_history.py",
    "infra/market_data/history/service_history_warmup.py",
    "infra/market_data/service/facade.py",
    "infra/market_data/service/service_calendar_state.py",
    "infra/market_data/service/service_candidates.py",
    "infra/market_data/service/service_execution.py",
    "infra/market_data/service/service_health.py",
    "infra/market_data/service/service_intraday.py",
    "infra/market_data/service/service_models.py",
    "infra/market_data/service/service_research.py",
    "infra/market_data/service/service_research_data_plane.py",
    "infra/market_data/service/service_research_models.py",
    "infra/market_data/service/service_tushare.py",
    "infra/research/history_automation_runtime.py",
    "infra/research/history_sync_runtime.py",
    "infra/runtime_support.py",
    "infra/scoring/profiles/v3/sample_store.py",
    "infra/settings/runtime.py",
)

FORBIDDEN_REPOSITORY_PATHS = ("scripts/runtime_diagnostics/common.py",)

FORBIDDEN_PUBLIC_NAMES = {
    "C3BaseModelFitPort",
    "C3CandidateEvaluator",
    "C3CandidateMetrics",
    "C3CandidateOOF",
    "C3CandidateTrainer",
    "C3DevelopmentResult",
    "C3OOFPrediction",
    "DeepSeekClientBase",
    "FittedBaseModels",
    "H1HTTPSession",
    "H1Strategy",
    "H1UniverseProvider",
    "HttpResponse",
    "ScoreFactorDiagnosticReport",
    "ScoreNativeFactorDiagnostics",
    "ScoreResearchCoverage",
    "ScoreResearchSpec",
    "ScoreResearchWindowCoverage",
    "ScoreTomorrowCostAwareSelection",
    "ScoreTomorrowPointInTimeFeatures",
    "ScoreTomorrowShadowModels",
    "ScoredBuildRuntime",
    "V3SampleStore",
    "V3StoredSample",
    "TomorrowC3Terminal",
    "TomorrowResearchPrerequisite",
    "TomorrowResearchPrerequisitePort",
    "TransparentCandidate",
    "TransparentCandidateEvaluation",
    "TransparentCandidateFamily",
    "TransparentCandidateMetrics",
    "TransparentCandidateReport",
}


def _active_python_paths() -> tuple[Path, ...]:
    return tuple(sorted((*SOURCE.rglob("*.py"), *(ROOT / "scripts").rglob("*.py"))))


def test_active_paths_use_stable_business_responsibilities() -> None:
    assert [relative for relative in FORBIDDEN_ACTIVE_PATHS if (SOURCE / relative).exists()] == []
    assert [relative for relative in FORBIDDEN_REPOSITORY_PATHS if (ROOT / relative).exists()] == []


def test_storage_responsibility_naming_rule_is_authoritative() -> None:
    collaboration_rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    design = (ROOT / "docs/02_工程设计.md").read_text(encoding="utf-8")

    for content in (collaboration_rules, design):
        assert "不得使用 `store`、`stored` 或 `*Store`" in content
        assert "Repository" in content


def test_active_python_has_no_c3_or_relative_holdout_identity_names() -> None:
    violations: list[str] = []
    token = re.compile(r"(?<![a-z0-9])c3(?![a-z0-9])", re.IGNORECASE)
    for path in _active_python_paths():
        content = path.read_text(encoding="utf-8")
        if token.search(content) or "new_holdout_" in content or "legacy_holdout_" in content:
            violations.append(path.relative_to(ROOT).as_posix())
    assert violations == []


def test_public_python_names_describe_business_roles() -> None:
    violations: list[str] = []
    for path in _active_python_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in FORBIDDEN_PUBLIC_NAMES
            ):
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{node.name}")
    assert violations == []


def test_tomorrow_training_sample_boundary_uses_repository_responsibility_names() -> None:
    paths = (
        SOURCE / "infra/scoring/profiles/v3/sample_builder.py",
        SOURCE / "infra/scoring/profiles/v3/model_fitting.py",
        SOURCE / "infra/scoring/profiles/v3/training_sample_repository.py",
    )
    violations: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: tuple[str, ...] = ()
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names = (node.name,)
            elif isinstance(node, ast.arg):
                names = (node.arg,)
            elif isinstance(node, ast.Name):
                names = (node.id,)
            for name in names:
                if "store" in name.lower():
                    violations.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{name}")
    assert violations == []


def test_active_python_text_has_no_accidental_duplicate_words_or_e1_phase_relic() -> None:
    violations: list[str] = []
    repeated_word = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*)\s+\1\b", re.IGNORECASE)
    for path in _active_python_paths():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            repeated = repeated_word.search(line)
            if (repeated is not None and repeated.group(1).lower() != "end") or re.search(r"\bE1\b", line):
                violations.append(f"{path.relative_to(ROOT).as_posix()}:{line_number}")
    assert violations == []


def test_shared_application_contract_names_are_unique() -> None:
    occurrences: dict[str, list[str]] = {
        "DataPlaneCoverage": [],
        "MarketChangeSet": [],
        "SelectionPolicy": [],
        "TradingCalendarPort": [],
    }
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in occurrences:
                occurrences[node.name].append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert {name: values for name, values in occurrences.items() if len(values) > 1} == {}
