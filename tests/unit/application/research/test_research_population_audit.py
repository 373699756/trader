from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.research_audit import (
    LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    V2CommittedResearchAudit,
    V2ResearchDecisionSetAudit,
    V2ResearchPopulationAudit,
    point_in_time_population_hash,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
RISK_FIELDS = (
    "major_shareholder_reduction",
    "financial_fraud_history",
    "official_investigation_history",
    "major_illegal_history",
    "fund_occupation_history",
    "illegal_guarantee_history",
    "forced_delisting_risk",
    "unlock_risk",
    "pledge_risk",
    "financial_deterioration",
)


def _population(observed_at: datetime) -> tuple[V2ResearchPopulationAudit, ...]:
    return (
        V2ResearchPopulationAudit(
            code="600001",
            board="main",
            industry="制造业",
            feature_observed_at=observed_at,
            quote_source_time=observed_at,
            quote_source="tencent",
            data_version="market-v1",
            is_st=True,
            listing_date=date(2010, 1, 1),
            is_relisted_first_session=False,
            is_delisting_period_first_session=True,
            has_delisting_name=True,
            structured_risk_values=tuple((name, None) for name in RISK_FIELDS),
            external_risk_facts=(),
            filter_reasons=("st_stock", "delisting_period"),
            disposition="reject",
            requested_for_refresh=False,
        ),
    )


def test_v2_local_audit_keeps_rejected_point_in_time_identity() -> None:
    observed_at = datetime(2026, 8, 28, 14, 49, tzinfo=SHANGHAI)
    population = _population(observed_at)
    decision_set = V2ResearchDecisionSetAudit("local-v1", ())

    audit = V2CommittedResearchAudit(
        decision_version="local-v1",
        decision_hash="decision-hash",
        input_version="input-v1",
        hard_filter_aggregates=(("main:st_stock", 1),),
        passed_candidates=(),
        production_local=decision_set,
        research_shadow=decision_set,
        shadow_mode="control_copy",
        input_observed_at=observed_at,
        point_in_time_population=population,
        point_in_time_population_hash=point_in_time_population_hash(population),
    )

    assert audit.point_in_time_population[0].disposition == "reject"
    assert audit.point_in_time_population[0].is_st is True
    assert audit.point_in_time_population[0].has_delisting_name is True


def test_v2_audit_rejects_population_evidence_after_input_time() -> None:
    input_at = datetime(2026, 8, 28, 14, 49, tzinfo=SHANGHAI)
    population = _population(input_at + timedelta(seconds=1))
    decision_set = V2ResearchDecisionSetAudit("local-v1", ())

    with pytest.raises(ValueError, match="after input_observed_at"):
        V2CommittedResearchAudit(
            decision_version="local-v1",
            decision_hash="decision-hash",
            input_version="input-v1",
            hard_filter_aggregates=(("main:st_stock", 1),),
            passed_candidates=(),
            production_local=decision_set,
            research_shadow=decision_set,
            shadow_mode="control_copy",
            input_observed_at=input_at,
            point_in_time_population=population,
            point_in_time_population_hash=point_in_time_population_hash(population),
        )


def test_legacy_v1_audit_remains_read_only_constructible() -> None:
    decision_set = V2ResearchDecisionSetAudit("local-v1", ())

    audit = V2CommittedResearchAudit(
        decision_version="local-v1",
        decision_hash="decision-hash",
        input_version="input-v1",
        hard_filter_aggregates=(("main:st_stock", 1),),
        passed_candidates=(),
        production_local=decision_set,
        research_shadow=decision_set,
        shadow_mode="control_copy",
        schema_version=LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    )

    assert audit.input_observed_at is None
    assert audit.point_in_time_population == ()
