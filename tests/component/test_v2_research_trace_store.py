from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
    ResearchTraceCapacityError,
    ResearchTraceConflictError,
    ResearchTraceLimits,
    SQLiteV2ResearchTraceStore,
)


def test_committed_event_trace_survives_restart_and_replays_idempotently(tmp_path) -> None:
    event = build_v2_decision_committed(decision())
    audit = _audit(event.decision_version, event.decision_hash)
    observation = V2DecisionObservation(event, audit)
    first = SQLiteV2ResearchTraceStore(tmp_path)
    first.initialize()

    first.record(observation)
    first.record(observation)

    reopened = SQLiteV2ResearchTraceStore(tmp_path)
    reopened.initialize()
    assert reopened.get(event.decision_version) == observation
    assert reopened.list_trade_dates(limit=4) == (event.trade_date,)
    assert reopened.list_by_trade_date(event.trade_date) == (observation,)
    assert reopened.status().retained == 1
    assert first.status().duplicate == 1


def test_committed_event_trace_reports_first_observation_per_trade_date(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path)
    base = decision()
    late = replace(base, sequence=2, observed_at=datetime(2026, 8, 11, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")))
    following = replace(
        base,
        sequence=3,
        trade_date=base.trade_date + timedelta(days=1),
        observed_at=datetime(2026, 8, 12, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    for item in (late, base, following):
        event = build_v2_decision_committed(item)
        store.record(V2DecisionObservation(event, None))

    observations = store.inspect_first_observations(limit=4)

    assert tuple(item.trade_date for item in observations) == (following.trade_date, base.trade_date)
    assert observations[0].observed_at == following.observed_at
    assert observations[1].observed_at == base.observed_at


def test_committed_event_trace_rejects_same_identity_with_different_payload(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path)
    store.initialize()
    event = build_v2_decision_committed(decision())
    store.record(V2DecisionObservation(event, _audit(event.decision_version, event.decision_hash)))

    with pytest.raises(ResearchTraceConflictError):
        store.record(V2DecisionObservation(replace(event, degraded_reasons=("conflicting_trace",)), None))


def test_committed_event_trace_quarantines_corrupt_rows(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path)
    store.initialize()
    event = build_v2_decision_committed(decision())
    store.record(V2DecisionObservation(event, _audit(event.decision_version, event.decision_hash)))
    database = tmp_path / "research" / "committed-events" / f"{event.trade_date.isoformat()}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE committed_events SET payload = ? WHERE decision_version = ?",
            (b"{}", event.decision_version),
        )

    reopened = SQLiteV2ResearchTraceStore(tmp_path)
    reopened.initialize()

    assert reopened.get(event.decision_version) is None
    assert reopened.status().quarantined == 1


def test_committed_event_trace_refuses_capacity_without_deleting_immutable_rows(tmp_path) -> None:
    store = SQLiteV2ResearchTraceStore(tmp_path, limits=ResearchTraceLimits(events_per_trade_date=1))
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
        limits=ResearchTraceLimits(payload_bytes=64, trade_date_bytes=128),
    )

    with pytest.raises(RuntimeError, match="payload capacity"):
        store.record(V2DecisionObservation(build_v2_decision_committed(decision()), None))

    assert store.status().retained == 0


def test_committed_event_trace_rejects_total_bytes_without_deleting_rows(tmp_path) -> None:
    first = V2DecisionObservation(build_v2_decision_committed(decision(sequence=1)), None)
    second = V2DecisionObservation(build_v2_decision_committed(decision(sequence=2)), None)
    probe = SQLiteV2ResearchTraceStore(tmp_path / "probe")
    probe.record(first)
    first_bytes = probe.status().retained_bytes
    store = SQLiteV2ResearchTraceStore(
        tmp_path / "bounded",
        limits=ResearchTraceLimits(payload_bytes=first_bytes + 1, trade_date_bytes=first_bytes + 1),
    )
    store.record(first)

    with pytest.raises(RuntimeError, match="capacity"):
        store.record(second)

    assert store.get(first.event.decision_version) == first
    assert store.status().retained == 1


def test_committed_event_trace_rotates_by_trade_date_and_preserves_full_legacy_database(tmp_path) -> None:
    first = V2DecisionObservation(build_v2_decision_committed(decision(sequence=1)), None)
    legacy = SQLiteV2ResearchTraceStore(tmp_path, use_legacy_layout=True)
    legacy.record(first)
    legacy_path = tmp_path / "research" / "committed-events.sqlite3"
    legacy_bytes = legacy_path.read_bytes()

    second_identity = decision(sequence=2)
    second_identity = replace(
        second_identity,
        trade_date=second_identity.trade_date + timedelta(days=1),
        observed_at=second_identity.observed_at + timedelta(days=1),
    )
    second = V2DecisionObservation(build_v2_decision_committed(second_identity), None)
    partitioned = SQLiteV2ResearchTraceStore(tmp_path)
    partitioned.record(second)

    assert legacy_path.read_bytes() == legacy_bytes
    assert partitioned.get(first.event.decision_version) == first
    assert partitioned.get(second.event.decision_version) == second
    assert partitioned.list_trade_dates(limit=4) == (second.event.trade_date, first.event.trade_date)
    assert partitioned.status().legacy_retained == 1
    assert partitioned.status().trade_dates == 2
    assert partitioned.status().trade_date_capacity == 120


def test_full_legacy_partition_does_not_block_a_new_trade_date(tmp_path) -> None:
    first = V2DecisionObservation(build_v2_decision_committed(decision(sequence=1)), None)
    probe = SQLiteV2ResearchTraceStore(tmp_path / "probe")
    probe.record(first)
    payload_bytes = probe.status().retained_bytes
    legacy = SQLiteV2ResearchTraceStore(
        tmp_path,
        limits=ResearchTraceLimits(payload_bytes=payload_bytes, trade_date_bytes=payload_bytes),
        use_legacy_layout=True,
    )
    legacy.record(first)

    next_identity = decision(sequence=2)
    next_identity = replace(
        next_identity,
        trade_date=next_identity.trade_date + timedelta(days=1),
        observed_at=next_identity.observed_at + timedelta(days=1),
    )
    partitioned = SQLiteV2ResearchTraceStore(
        tmp_path,
        limits=ResearchTraceLimits(
            payload_bytes=payload_bytes,
            trade_date_bytes=payload_bytes,
            archive_bytes=payload_bytes * 3,
        ),
    )
    partitioned.record(V2DecisionObservation(build_v2_decision_committed(next_identity), None))

    assert partitioned.status().retained == 2
    assert partitioned.status().remaining_bytes > 0


def test_archive_capacity_rejects_a_new_partition_without_creating_an_empty_database(tmp_path) -> None:
    first = V2DecisionObservation(build_v2_decision_committed(decision(sequence=1)), None)
    probe = SQLiteV2ResearchTraceStore(tmp_path / "probe")
    probe.record(first)
    payload_bytes = probe.status().retained_bytes
    store = SQLiteV2ResearchTraceStore(
        tmp_path,
        limits=ResearchTraceLimits(
            payload_bytes=payload_bytes + 1,
            trade_date_bytes=payload_bytes + 1,
            archive_bytes=payload_bytes + 1,
        ),
    )
    store.record(first)
    next_identity = replace(
        decision(sequence=2),
        trade_date=first.event.trade_date + timedelta(days=1),
        observed_at=first.event.observed_at + timedelta(days=1),
    )

    with pytest.raises(ResearchTraceCapacityError, match="archive capacity"):
        store.record(V2DecisionObservation(build_v2_decision_committed(next_identity), None))

    rejected_partition = tmp_path / "research" / "committed-events" / f"{next_identity.trade_date.isoformat()}.sqlite3"
    assert not rejected_partition.exists()
    assert store.status().retained == 1


def test_formal_replay_without_audit_preserves_existing_committed_audit(tmp_path) -> None:
    event = build_v2_decision_committed(decision())
    audit = _audit(event.decision_version, event.decision_hash)
    store = SQLiteV2ResearchTraceStore(tmp_path)
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
