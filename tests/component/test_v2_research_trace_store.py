from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from tests.unit.domain.test_decision_identity import decision
from trader.application.decision_events import build_v2_decision_committed
from trader.application.research_audit import (
    V2CommittedResearchAudit,
    V2DecisionObservation,
    V2ResearchCandidateAudit,
    V2ResearchDecisionCandidateAudit,
    V2ResearchDecisionSetAudit,
)
from trader.infra.persistence.research_trace import (
    ResearchTraceConflictError,
    SQLiteV2ResearchTraceStore,
)


def test_committed_event_trace_survives_restart_and_replays_idempotently(tmp_path) -> None:
    event = build_v2_decision_committed(decision())
    audit = _audit(event.decision_version, event.decision_hash)
    observation = V2DecisionObservation(event, audit)
    first = SQLiteV2ResearchTraceStore(tmp_path, capacity=16)
    first.initialize()

    first.record(observation)
    first.record(observation)

    reopened = SQLiteV2ResearchTraceStore(tmp_path, capacity=16)
    reopened.initialize()
    assert reopened.get(event.decision_version) == observation
    assert reopened.list_trade_dates(limit=4) == (event.trade_date,)
    assert reopened.list_by_trade_date(event.trade_date) == (observation,)
    assert reopened.status().retained == 1
    assert first.status().duplicate == 1


def test_committed_event_trace_rejects_same_identity_with_different_payload(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path, capacity=16)
    store.initialize()
    event = build_v2_decision_committed(decision())
    store.record(V2DecisionObservation(event, _audit(event.decision_version, event.decision_hash)))

    with pytest.raises(ResearchTraceConflictError):
        store.record(V2DecisionObservation(replace(event, degraded_reasons=("conflicting_trace",)), None))


def test_committed_event_trace_quarantines_corrupt_rows(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path, capacity=16)
    store.initialize()
    event = build_v2_decision_committed(decision())
    store.record(V2DecisionObservation(event, _audit(event.decision_version, event.decision_hash)))
    database = tmp_path / "research" / "committed-events.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE committed_events SET payload = ? WHERE decision_version = ?",
            (b"{}", event.decision_version),
        )

    reopened = SQLiteV2ResearchTraceStore(tmp_path, capacity=16)
    reopened.initialize()

    assert reopened.get(event.decision_version) is None
    assert reopened.status().quarantined == 1


def test_committed_event_trace_refuses_capacity_without_deleting_immutable_rows(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path, capacity=1)
    store.initialize()
    first = build_v2_decision_committed(decision(sequence=1))
    store.record(V2DecisionObservation(first, _audit(first.decision_version, first.decision_hash)))

    with pytest.raises(RuntimeError, match="capacity"):
        second = build_v2_decision_committed(decision(sequence=2))
        store.record(V2DecisionObservation(second, _audit(second.decision_version, second.decision_hash)))

    assert store.status().retained == 1


def test_committed_event_trace_rejects_payload_over_byte_limit(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(
        tmp_path,
        capacity=16,
        maximum_payload_bytes=64,
        maximum_total_bytes=128,
    )

    with pytest.raises(RuntimeError, match="payload capacity"):
        store.record(V2DecisionObservation(build_v2_decision_committed(decision()), None))

    assert store.status().retained == 0


def test_committed_event_trace_rejects_total_bytes_without_deleting_rows(tmp_path) -> None:
    first = V2DecisionObservation(build_v2_decision_committed(decision(sequence=1)), None)
    second = V2DecisionObservation(build_v2_decision_committed(decision(sequence=2)), None)
    probe = SQLiteV2ResearchTraceStore(tmp_path / "probe", capacity=16)
    probe.record(first)
    first_bytes = probe.status().retained_bytes
    store = SQLiteV2ResearchTraceStore(
        tmp_path / "bounded",
        capacity=16,
        maximum_payload_bytes=first_bytes + 1,
        maximum_total_bytes=first_bytes + 1,
    )
    store.record(first)

    with pytest.raises(RuntimeError, match="capacity"):
        store.record(second)

    assert store.get(first.event.decision_version) == first
    assert store.status().retained == 1


def test_formal_replay_without_audit_preserves_existing_committed_audit(tmp_path) -> None:
    event = build_v2_decision_committed(decision())
    audit = _audit(event.decision_version, event.decision_hash)
    store = SQLiteV2ResearchTraceStore(tmp_path, capacity=16)
    store.initialize()
    store.record(V2DecisionObservation(event, audit))

    store.record(V2DecisionObservation(event, None))

    assert store.get(event.decision_version) == V2DecisionObservation(event, audit)
    assert store.status().duplicate == 1


def test_committed_research_audit_rejects_decisions_outside_passed_population() -> None:
    event = build_v2_decision_committed(decision())
    audit = _audit(event.decision_version, event.decision_hash)

    with pytest.raises(ValueError, match="hard-filter passed population"):
        replace(audit, passed_candidates=())


def _audit(decision_version: str, decision_hash: str) -> V2CommittedResearchAudit:
    decision_set = V2ResearchDecisionSetAudit(
        decision_version=decision_version,
        candidates=(
            V2ResearchDecisionCandidateAudit(
                code="600001",
                components=(("trend", 88.0),),
                component_coverage_ratio=1.0,
                base_score=88.0,
                local_risk_codes=(),
                local_risk_penalty=0.0,
                local_score=88.0,
                reused_deepseek_facts=False,
                fusion_applied=False,
                deepseek_risk_codes=(),
                deepseek_risk_penalty=0.0,
                final_score=88.0,
                action="executable",
                selected=True,
                rank=1,
                board_rank=1,
                skip_reason="selected",
            ),
        ),
    )
    return V2CommittedResearchAudit(
        decision_version=decision_version,
        decision_hash=decision_hash,
        input_version="native:v1",
        hard_filter_aggregates=(("main:st_or_delisting", 2),),
        passed_candidates=(
            V2ResearchCandidateAudit(
                code="600001",
                board="main",
                industry="equipment",
                candidate_components=(("liquidity", 80.0),),
                missing_mask=(),
                coverage_ratio=1.0,
                board_reliability=1.0,
                candidate_score=84.0,
                candidate_rank=1,
                production_top120=True,
                preselection_status="selected_for_full_scoring",
            ),
        ),
        production_local=decision_set,
        research_shadow=decision_set,
        shadow_mode="control_copy",
    )
