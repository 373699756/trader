from __future__ import annotations

import base64
import json
from pathlib import Path
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.training.application.research_audit import (
    CommittedResearchAudit,
    HistoricalResearchAudit,
    HasCompleteResearchEvidence,
    PointInTimePopulationHash,
    ResearchFreezeSetAudit,
    ResearchPopulationAudit,
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


def _population(observed_at: datetime) -> tuple[ResearchPopulationAudit, ...]:
    return (
        ResearchPopulationAudit(
            code="600001",
            board="main",
            industry="制造业",
            feature_observed_at=observed_at,
            quote_source_time=observed_at,
            quote_source="tencent",
            data_version="market-initial",
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


def test_local_audit_keeps_rejected_point_in_time_identity() -> None:
    observed_at = datetime(2026, 8, 28, 14, 49, tzinfo=SHANGHAI)
    population = _population(observed_at)
    freeze_set = ResearchFreezeSetAudit("local-freeze", ())

    audit = CommittedResearchAudit(
        freeze_version="local-freeze",
        freeze_hash="freeze-hash",
        input_version="input-initial",
        hard_filter_aggregates=(("main:st_stock", 1),),
        passed_candidates=(),
        production_local=freeze_set,
        research_shadow=freeze_set,
        shadow_mode="control_copy",
        input_observed_at=observed_at,
        point_in_time_population=population,
        point_in_time_population_hash=PointInTimePopulationHash(population),
    )

    assert audit.point_in_time_population[0].disposition == "reject"
    assert audit.point_in_time_population[0].is_st is True
    assert audit.point_in_time_population[0].has_delisting_name is True


def test_audit_rejects_population_evidence_after_input_time() -> None:
    input_at = datetime(2026, 8, 28, 14, 49, tzinfo=SHANGHAI)
    population = _population(input_at + timedelta(seconds=1))
    freeze_set = ResearchFreezeSetAudit("local-freeze", ())

    with pytest.raises(ValueError, match="after input_observed_at"):
        CommittedResearchAudit(
            freeze_version="local-freeze",
            freeze_hash="freeze-hash",
            input_version="input-initial",
            hard_filter_aggregates=(("main:st_stock", 1),),
            passed_candidates=(),
            production_local=freeze_set,
            research_shadow=freeze_set,
            shadow_mode="control_copy",
            input_observed_at=input_at,
            point_in_time_population=population,
            point_in_time_population_hash=PointInTimePopulationHash(population),
        )


def test_saved_incomplete_audit_keeps_original_hash_and_missing_evidence() -> None:
    from trader.recommendation.infra.persistence.freeze_trace_index import _observation_from_bytes

    path = Path(__file__).parents[4] / "fixtures/recommendation/history_553e9cc0.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    payload = base64.b64decode(saved["legacy_event_base64"])
    observation = _observation_from_bytes(payload, saved["legacy_event_hash"])
    audit = observation.research_audit

    assert isinstance(audit, HistoricalResearchAudit)
    assert not isinstance(audit, CommittedResearchAudit)
    assert audit.input_observed_at is None
    assert audit.point_in_time_population is None
    assert audit.point_in_time_population_hash is None
    assert not HasCompleteResearchEvidence(audit)
    assert observation.event_bytes == payload
    assert observation.event_hash == saved["legacy_event_hash"]


def test_current_audit_rejects_missing_point_in_time_evidence() -> None:
    freeze_set = ResearchFreezeSetAudit("local-freeze", ())
    with pytest.raises(ValueError, match="input_observed_at"):
        CommittedResearchAudit(
            freeze_version="local-freeze", freeze_hash="freeze-hash", input_version="input-initial",
            hard_filter_aggregates=(), passed_candidates=(), production_local=freeze_set,
            research_shadow=freeze_set, shadow_mode="control_copy",
        )
