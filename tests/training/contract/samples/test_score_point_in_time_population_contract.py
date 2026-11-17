from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_research_audit_keeps_complete_point_in_time_population() -> None:
    source = (ROOT / "src/trader/training/application/research_audit.py").read_text(encoding="utf-8")

    for token in (
        "class HistoricalResearchAudit:",
        "def HasCompleteResearchEvidence(",
        "class ResearchPopulationAudit:",
        "point_in_time_population:",
        "point_in_time_population_hash:",
        "input_observed_at:",
        "structured_risk_values:",
        "external_risk_facts:",
    ):
        assert token in source


def test_research_trace_has_read_only_original_projection_and_cutoff_read() -> None:
    source = (ROOT / "src/trader/training/infra/research/research_trace_index.py").read_text(encoding="utf-8")
    source += (ROOT / "src/trader/recommendation/infra/persistence/freeze_trace_index.py").read_text(encoding="utf-8")

    for token in (
        "HistoricalFreezeObservation",
        "HistoricalResearchAudit",
        "HasCompleteResearchEvidence",
        "def latest_point_in_time_observation(",
        "cutoff: time = time(hour=15, minute=0)",
        "point_in_time_population_hash",
    ):
        assert token in source
