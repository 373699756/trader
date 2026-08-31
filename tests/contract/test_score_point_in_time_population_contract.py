from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_research_audit_keeps_complete_point_in_time_population() -> None:
    source = (ROOT / "src/trader/application/research/research_audit.py").read_text(encoding="utf-8")

    for token in (
        'RESEARCH_AUDIT_SCHEMA_VERSION = "v2_committed_research_audit_v2"',
        'LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION = "v2_committed_research_audit_v1"',
        "class V2ResearchPopulationAudit:",
        "point_in_time_population:",
        "point_in_time_population_hash:",
        "input_observed_at:",
        "structured_risk_values:",
        "external_risk_facts:",
    ):
        assert token in source


def test_research_trace_has_explicit_legacy_codec_and_cutoff_read() -> None:
    source = (ROOT / "src/trader/infra/persistence/research_trace.py").read_text(encoding="utf-8")

    for token in (
        'RESEARCH_EVENT_SCHEMA_VERSION = "v2_research_committed_event_v2"',
        'LEGACY_RESEARCH_EVENT_SCHEMA_VERSION = "v2_research_committed_event_v1"',
        "def latest_point_in_time_observation(",
        "cutoff: time = time(hour=14, minute=50)",
        "point_in_time_population_hash",
    ):
        assert token in source
