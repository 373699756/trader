from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/trader"


def test_production_tree_has_no_forward_score_validation_owner() -> None:
    retired_paths = (
        SOURCE / "domain/research/tomorrow_profile_comparison.py",
        SOURCE / "application/research/tomorrow_profile_comparison.py",
        SOURCE / "application/research/tomorrow_profile_reporting.py",
        SOURCE / "application/research/tomorrow_profile_settlement.py",
        SOURCE / "infra/persistence/tomorrow_profile_comparison.py",
        SOURCE / "infra/persistence/tomorrow_profile_comparison_codec.py",
        SOURCE / "infra/research/forward_evidence.py",
        SOURCE / "infra/research/preregistered_shadow_artifacts.py",
    )
    assert not [path for path in retired_paths if path.exists()]

    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SOURCE / "bootstrap.py",
            SOURCE / "recommendation/infra/status_projection.py",
            SOURCE / "recommendation/application/pipeline/data_source/source_router.py",
            SOURCE / "recommendation/application/ports/runtime.py",
            SOURCE / "http_api/routes/page_routes.py",
        )
    )
    for retired in (
        "TomorrowProfileComparator",
        "TomorrowProfileSettlementService",
        "tomorrow_profile_comparison",
        "tomorrow_profile_research_input",
    ):
        assert retired not in production


def test_online_outcomes_remain_monitoring_only_without_changing_scoring_or_freeze() -> None:
    strategy = (ROOT / "docs/1_评分逻辑.md").read_text(encoding="utf-8")
    bootstrap = (SOURCE / "bootstrap.py").read_text(encoding="utf-8")

    for invariant in (
        "final_score = ROUND_HALF_UP(clamp(base_score - local_risk_penalty, 0, 100), 2)",
        "15:00",
    ):
        assert invariant in strategy
    assert "OutcomeSettlementService" in bootstrap
    assert "profile_factory.BuildProfileScoring" in bootstrap
