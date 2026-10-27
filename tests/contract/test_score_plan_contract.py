from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/trader"


def _compact(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_forward_score_validation_owners_and_commands_are_retired() -> None:
    retired_paths = (
        SOURCE / "application/research/score_r5.py",
        SOURCE / "application/research/score_r7.py",
        SOURCE / "application/research/preregistered_shadow.py",
        SOURCE / "application/research/tomorrow_profile_comparison.py",
        SOURCE / "application/research/tomorrow_profile_reporting.py",
        SOURCE / "application/research/tomorrow_profile_settlement.py",
        SOURCE / "domain/research/tomorrow_profile_comparison.py",
        SOURCE / "infra/research/forward_evidence.py",
        SOURCE / "infra/research/score_r7_artifacts.py",
        SOURCE / "infra/persistence/tomorrow_profile_comparison.py",
    )
    assert not [path for path in retired_paths if path.exists()]

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SOURCE / "entrypoints/cli.py",
            ROOT / "run.sh",
            ROOT / "run.ps1",
        )
    )
    assert "research-r7-dossier" not in combined
    assert "research-tomorrow-profile-report" not in combined


def test_remaining_offline_research_is_historical_and_production_isolated() -> None:
    design = _compact(ROOT / "docs/2_工程设计.md")
    research_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SOURCE / "training/application/tomorrow_historical_screening.py",
            SOURCE / "training/application/tomorrow_point_in_time_holdout.py",
            SOURCE / "training/application/tomorrow_historical_validation.py",
        )
    )

    for token in ("tomorrow_historical", "TomorrowPointInTimeHoldout", "tomorrow_historical_risk_probability"):
        assert token in research_sources
    assert "download" in design
    assert "./run.sh train" in design


def test_research_and_manual_production_override_remain_explicit() -> None:
    strategy = _compact(ROOT / "docs/1_评分逻辑.md")

    for token in (
        "manual_user_override",
        "loss_probability_status=not_modeled",
    ):
        assert token in strategy
    runtime_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SOURCE / "training/domain/evaluation/tomorrow_historical.py",
            SOURCE / "recommendation/application/ports/loaded_profile.py",
        )
    )
    for token in ("daily_reconstructible_ensemble", "single_candidate_pass_or_stop", "historical_ohlcv_cross_section"):
        assert token in runtime_sources
    assert "automatic_t1_outcome_settlement" in runtime_sources
    artifact_hash = "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"
    assert artifact_hash not in strategy


def test_historical_evidence_does_not_create_a_running_collection_gate() -> None:
    strategy = _compact(ROOT / "docs/1_评分逻辑.md")
    design = _compact(ROOT / "docs/2_工程设计.md")
    holdout = _compact(SOURCE / "training/application/tomorrow_point_in_time_holdout.py")

    assert "TomorrowPointInTimeHoldout" in holdout
    for retired in ("522 个有效交易日", "tomorrow_v1_v2_paired_forward_v1"):
        assert retired not in strategy
        assert retired not in design
