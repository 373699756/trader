from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest

from tests.unit.domain.test_freeze_identity import freeze
from trader.infra.sqlite_codec import EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import (
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    BuildFreezeCommitted,
    FreezeObservation,
)
from trader.recommendation.infra.persistence.freeze_trace_index import (
    LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
    ResearchTraceConflictError,
    ResearchTraceLimits,
    SQLiteFreezeTraceIndex,
)
from trader.recommendation.infra.persistence.trace_capacity import ResearchTraceCapacityError
from trader.recommendation.infra.persistence.trace_projection import ObservationBytes
from trader.training.application.research_audit import (
    LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    CommittedResearchAudit,
    PointInTimePopulationHash,
    ResearchCandidateAudit,
    ResearchFreezeCandidateAudit,
    ResearchFreezeSetAudit,
    ResearchPopulationAudit,
)
from trader.training.infra.research.research_trace_index import ResearchTraceIndex


def _persistent_event(value):
    return FreezeObservation(BuildFreezeCommitted(value), None).materialize().event


@pytest.mark.parametrize("limit", ("physical_trade_date_bytes", "physical_total_bytes"))
def test_physical_capacity_rejection_preserves_old_audit_and_allows_duplicate(tmp_path, limit):
    first = FreezeObservation(_persistent_event(freeze()), None)
    existing = SQLiteFreezeTraceIndex(tmp_path)
    existing.record(first)
    restricted = SQLiteFreezeTraceIndex(tmp_path, limits=replace(ResearchTraceLimits(), **{limit: 1}))
    restricted.record(first)
    with pytest.raises(ResearchTraceCapacityError, match="physical"):
        restricted.record(FreezeObservation(_persistent_event(freeze(sequence=2)), None))
    assert existing.get(first.event.freeze_version) == first
    assert existing.status().retained == 1


def test_physical_capacity_counts_wal_and_rejects_before_partition_creation(tmp_path):
    from trader.recommendation.infra.persistence.trace_capacity import TracePhysicalBudget

    database = tmp_path / "audit.sqlite3"
    database.write_bytes(b"a" * 4096)
    wal = tmp_path / "audit.sqlite3-wal"
    wal.write_bytes(b"b" * 16384)
    with pytest.raises(ResearchTraceCapacityError, match="physical partition"):
        TracePhysicalBudget(8192, 65536).require((database,), database)
    hardlink = tmp_path / "same.sqlite3"
    hardlink.hardlink_to(database)
    physical = sum(max(path.stat().st_size, getattr(path.stat(), "st_blocks", 0) * 512) for path in (database, wal))
    TracePhysicalBudget(physical, physical).require((database, hardlink), database)
    limited = SQLiteFreezeTraceIndex(tmp_path / "limited", limits=ResearchTraceLimits(physical_total_bytes=1))
    with pytest.raises(ResearchTraceCapacityError, match="physical total"):
        limited.record(FreezeObservation(_persistent_event(freeze()), None))
    assert not tuple((tmp_path / "limited").rglob("*.sqlite3"))


def test_physical_commit_admission_rolls_back_index_and_payload_growth(tmp_path):
    from trader.recommendation.infra.persistence.trace_capacity import TracePhysicalBudget

    database = tmp_path / "audit.sqlite3"
    with SqliteSession(database, SqliteConnectionOptions(journal_mode="WAL")) as conn, SqliteTransaction(conn):
        conn.run("CREATE TABLE audit (id INTEGER PRIMARY KEY, event_bytes BLOB)")
        conn.run("CREATE INDEX payload_idx ON audit(event_bytes)")
        conn.run("INSERT INTO audit VALUES(1, 'old')")
    size_before = database.stat().st_size
    with pytest.raises(ResearchTraceCapacityError, match="physical"):
        with SqliteSession(database) as conn, SqliteTransaction(conn, immediate=True):
            conn.run("PRAGMA cache_spill=OFF")
            conn.run("INSERT INTO audit VALUES(2, zeroblob(262144))")
            TracePhysicalBudget(131072, 131072).require_commit(conn, (database,), database)
    with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as conn:
        assert conn.run("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    assert database.stat().st_size == size_before


def test_committed_event_trace_survives_restart_and_replays_idempotently(tmp_path) -> None:
    event = _persistent_event(freeze())
    audit = _audit(event.freeze_version, event.freeze_hash)
    observation = FreezeObservation(event, audit)
    first = SQLiteFreezeTraceIndex(tmp_path)
    first.initialize()

    first.record(observation)
    first.record(observation)

    reopened = SQLiteFreezeTraceIndex(tmp_path)
    reopened.initialize()
    assert reopened.get(event.freeze_version) == observation
    assert reopened.list_trade_dates(limit=4) == (event.trade_date,)
    assert reopened.list_by_trade_date(event.trade_date) == (observation,)
    assert reopened.status().retained == 1
    assert first.status().duplicate == 1


def test_legacy_v1_event_is_readable_but_cannot_be_written_by_current_archive(tmp_path) -> None:
    event = _persistent_event(freeze())
    legacy_audit = _legacy_audit(event.freeze_version, event.freeze_hash)
    observation = FreezeObservation(event, legacy_audit)
    archive = SQLiteFreezeTraceIndex(tmp_path)
    archive.initialize()

    with pytest.raises(ValueError, match="read-only"):
        archive.record(observation)

    payload = ObservationBytes(
        observation,
        schema_version=LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
        legacy_schema_version=LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
        current_schema_version="research_committed_event",
    )
    database = tmp_path / "traces" / f"{event.trade_date.isoformat()}.sqlite3"
    from trader.recommendation.infra.persistence.trace_schema import InitializeDatabase

    InitializeDatabase(database, set())
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        connection.run(
            """
            INSERT INTO committed_events (
                freeze_id, strategy, trade_date, observed_at,
                freeze_hash, schema_version, event_hash, event_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.freeze_version,
                event.strategy.value,
                EncodeDate(event.trade_date),
                EncodeTime(event.observed_at),
                EncodeHash(event.freeze_hash),
                LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
                sha256(payload).digest(),
                payload,
            ),
        )

    assert SQLiteFreezeTraceIndex(tmp_path).get(event.freeze_version) == observation


def test_point_in_time_population_survives_restart_and_obeys_cutoff(tmp_path) -> None:
    before = replace(
        freeze(sequence=1),
        observed_at=datetime(2026, 8, 11, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    before_event = _persistent_event(before)
    before_audit = _point_in_time_audit(before_event.freeze_version, before_event.freeze_hash, before.observed_at)
    before_observation = FreezeObservation(before_event, before_audit)
    late = replace(
        before,
        sequence=2,
        observed_at=datetime(2026, 8, 11, 15, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    late_event = _persistent_event(late)
    late_observation = FreezeObservation(
        late_event,
        _point_in_time_audit(late_event.freeze_version, late_event.freeze_hash, late.observed_at),
    )
    archive = SQLiteFreezeTraceIndex(tmp_path)
    archive.record(before_observation)
    archive.record(late_observation)

    reopened = SQLiteFreezeTraceIndex(tmp_path)

    assert reopened.get(before_event.freeze_version) == before_observation
    assert (
        ResearchTraceIndex(reopened.list_by_trade_date).latest_point_in_time_observation(
            before.trade_date, before.strategy
        )
        == before_observation
    )


def test_committed_event_trace_reports_first_observation_per_trade_date(tmp_path) -> None:
    archive = SQLiteFreezeTraceIndex(tmp_path)
    base = freeze()
    late = replace(base, sequence=2, observed_at=datetime(2026, 8, 11, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")))
    following = replace(
        base,
        sequence=3,
        trade_date=base.trade_date + timedelta(days=1),
        observed_at=datetime(2026, 8, 12, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    for item in (late, base, following):
        event = _persistent_event(item)
        archive.record(FreezeObservation(event, None))

    observations = archive.inspect_first_observations(limit=4)

    assert tuple(item.trade_date for item in observations) == (following.trade_date, base.trade_date)
    assert observations[0].observed_at == following.observed_at
    assert observations[1].observed_at == base.observed_at


def test_committed_event_trace_rejects_same_identity_with_different_payload(tmp_path) -> None:
    archive = SQLiteFreezeTraceIndex(tmp_path)
    archive.initialize()
    event = _persistent_event(freeze())
    archive.record(FreezeObservation(event, _audit(event.freeze_version, event.freeze_hash)))

    with pytest.raises(ResearchTraceConflictError):
        archive.record(FreezeObservation(replace(event, degraded_reasons=("conflicting_trace",)), None))


def test_trace_reads_preserve_corrupt_bytes_and_writer_quarantines(tmp_path) -> None:
    archive = SQLiteFreezeTraceIndex(tmp_path)
    archive.initialize()
    event = _persistent_event(freeze())
    archive.record(FreezeObservation(event, _audit(event.freeze_version, event.freeze_hash)))
    database = tmp_path / "traces" / f"{event.trade_date.isoformat()}.sqlite3"
    with SqliteSession(database) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE committed_events SET event_bytes = ? WHERE freeze_id = ?",
            (b"{}", event.freeze_version),
        )

    reopened = SQLiteFreezeTraceIndex(tmp_path)
    reopened.initialize()

    before = database.read_bytes()
    with pytest.raises(ResearchTraceConflictError, match="verification"):
        reopened.get(event.freeze_version)
    assert database.read_bytes() == before
    assert reopened.status().quarantined == 0
    next_event = _persistent_event(freeze(sequence=2))
    reopened.record(FreezeObservation(next_event, None))
    assert reopened.get(event.freeze_version) is None
    assert reopened.status().quarantined == 1


def test_committed_event_trace_refuses_capacity_without_deleting_immutable_rows(tmp_path) -> None:
    archive = SQLiteFreezeTraceIndex(tmp_path, limits=ResearchTraceLimits(events_per_trade_date=1))
    archive.initialize()
    first = _persistent_event(freeze(sequence=1))
    archive.record(FreezeObservation(first, _audit(first.freeze_version, first.freeze_hash)))

    with pytest.raises(RuntimeError, match="capacity"):
        second = _persistent_event(freeze(sequence=2))
        archive.record(FreezeObservation(second, _audit(second.freeze_version, second.freeze_hash)))

    assert archive.status().retained == 1


def test_committed_event_trace_rejects_payload_over_byte_limit(tmp_path) -> None:
    archive = SQLiteFreezeTraceIndex(
        tmp_path,
        limits=ResearchTraceLimits(payload_bytes=64, trade_date_bytes=128),
    )

    with pytest.raises(RuntimeError, match="payload capacity"):
        archive.record(FreezeObservation(_persistent_event(freeze()), None))

    assert archive.status().retained == 0


def test_full_market_research_audit_is_compressed_and_survives_restart(tmp_path) -> None:
    event = _persistent_event(freeze())
    audit = _point_in_time_audit(event.freeze_version, event.freeze_hash, event.observed_at)
    template = audit.point_in_time_population[0]
    population = tuple(
        sorted(
            (
                template,
                *(
                    replace(template, code=f"{100000 + index:06d}", requested_for_refresh=index < 360)
                    for index in range(5_290)
                ),
            ),
            key=lambda item: item.code,
        )
    )
    expanded_audit = replace(
        audit,
        point_in_time_population=population,
        point_in_time_population_hash=PointInTimePopulationHash(population),
    )
    observation = FreezeObservation(event, expanded_audit)
    logical_payload = ObservationBytes(
        observation,
        schema_version="research_committed_event",
        legacy_schema_version=LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
        current_schema_version="research_committed_event",
    )
    assert len(logical_payload) > 4 * 1024 * 1024

    archive = SQLiteFreezeTraceIndex(tmp_path)
    archive.record(observation)

    assert 0 < archive.status().retained_bytes < 4 * 1024 * 1024
    assert SQLiteFreezeTraceIndex(tmp_path).get(event.freeze_version) == observation


def test_research_trace_rejects_compressed_payload_over_decoded_limit(tmp_path) -> None:
    archive = SQLiteFreezeTraceIndex(
        tmp_path,
        limits=ResearchTraceLimits(
            payload_bytes=4096,
            decoded_payload_bytes=64,
            trade_date_bytes=8192,
        ),
    )

    with pytest.raises(ResearchTraceCapacityError, match="decoded payload capacity"):
        archive.record(FreezeObservation(_persistent_event(freeze()), None))


def test_committed_event_trace_rejects_total_bytes_without_deleting_rows(tmp_path) -> None:
    first = FreezeObservation(_persistent_event(freeze(sequence=1)), None)
    second = FreezeObservation(_persistent_event(freeze(sequence=2)), None)
    probe = SQLiteFreezeTraceIndex(tmp_path / "probe")
    probe.record(first)
    first_bytes = probe.status().retained_bytes
    archive = SQLiteFreezeTraceIndex(
        tmp_path / "bounded",
        limits=ResearchTraceLimits(payload_bytes=first_bytes + 1, trade_date_bytes=first_bytes + 1),
    )
    archive.record(first)

    with pytest.raises(RuntimeError, match="capacity"):
        archive.record(second)

    assert archive.get(first.event.freeze_version) == first
    assert archive.status().retained == 1


def test_committed_event_trace_rotates_by_trade_date_and_preserves_previous_partition(tmp_path) -> None:
    first = FreezeObservation(_persistent_event(freeze(sequence=1)), None)
    legacy = SQLiteFreezeTraceIndex(tmp_path)
    legacy.record(first)
    legacy_path = tmp_path / "traces" / f"{first.event.trade_date.isoformat()}.sqlite3"
    legacy_bytes = legacy_path.read_bytes()

    second_identity = freeze(sequence=2)
    second_identity = replace(
        second_identity,
        trade_date=second_identity.trade_date + timedelta(days=1),
        observed_at=second_identity.observed_at + timedelta(days=1),
    )
    second = FreezeObservation(_persistent_event(second_identity), None)
    partitioned = SQLiteFreezeTraceIndex(tmp_path)
    partitioned.record(second)

    assert legacy_path.read_bytes() == legacy_bytes
    assert partitioned.get(first.event.freeze_version) == first
    assert partitioned.get(second.event.freeze_version) == second
    assert partitioned.list_trade_dates(limit=4) == (second.event.trade_date, first.event.trade_date)
    assert partitioned.status().legacy_retained == 0
    assert partitioned.status().trade_dates == 2
    assert partitioned.status().trade_date_capacity == 120


def test_full_previous_partition_does_not_block_a_new_trade_date(tmp_path) -> None:
    first = FreezeObservation(_persistent_event(freeze(sequence=1)), None)
    probe = SQLiteFreezeTraceIndex(tmp_path / "probe")
    probe.record(first)
    payload_bytes = probe.status().retained_bytes
    legacy = SQLiteFreezeTraceIndex(
        tmp_path,
        limits=ResearchTraceLimits(payload_bytes=payload_bytes, trade_date_bytes=payload_bytes),
    )
    legacy.record(first)

    next_identity = freeze(sequence=2)
    next_identity = replace(
        next_identity,
        trade_date=next_identity.trade_date + timedelta(days=1),
        observed_at=next_identity.observed_at + timedelta(days=1),
    )
    partitioned = SQLiteFreezeTraceIndex(
        tmp_path,
        limits=ResearchTraceLimits(
            payload_bytes=payload_bytes,
            trade_date_bytes=payload_bytes,
            total_bytes=payload_bytes * 3,
        ),
    )
    partitioned.record(FreezeObservation(_persistent_event(next_identity), None))

    assert partitioned.status().retained == 2
    assert partitioned.status().remaining_bytes > 0


def test_archive_capacity_rejects_a_new_partition_without_creating_an_empty_database(tmp_path) -> None:
    first = FreezeObservation(_persistent_event(freeze(sequence=1)), None)
    probe = SQLiteFreezeTraceIndex(tmp_path / "probe")
    probe.record(first)
    payload_bytes = probe.status().retained_bytes
    archive = SQLiteFreezeTraceIndex(
        tmp_path,
        limits=ResearchTraceLimits(
            payload_bytes=payload_bytes + 1,
            trade_date_bytes=payload_bytes + 1,
            total_bytes=payload_bytes + 1,
        ),
    )
    archive.record(first)
    next_identity = replace(
        freeze(sequence=2),
        trade_date=first.event.trade_date + timedelta(days=1),
        observed_at=first.event.observed_at + timedelta(days=1),
    )

    with pytest.raises(ResearchTraceCapacityError, match="total capacity"):
        archive.record(FreezeObservation(_persistent_event(next_identity), None))

    rejected_partition = tmp_path / "traces" / f"{next_identity.trade_date.isoformat()}.sqlite3"
    assert not rejected_partition.exists()
    assert archive.status().retained == 1


def test_formal_replay_without_audit_preserves_existing_committed_audit(tmp_path) -> None:
    event = _persistent_event(freeze())
    audit = _audit(event.freeze_version, event.freeze_hash)
    archive = SQLiteFreezeTraceIndex(tmp_path)
    archive.initialize()
    archive.record(FreezeObservation(event, audit))

    archive.record(FreezeObservation(event, None))

    assert archive.get(event.freeze_version) == FreezeObservation(event, audit)
    assert archive.status().duplicate == 1


def test_committed_research_audit_rejects_freezes_outside_passed_population() -> None:
    event = _persistent_event(freeze())
    audit = _audit(event.freeze_version, event.freeze_hash)

    with pytest.raises(ValueError, match="hard-filter passed population"):
        replace(audit, passed_candidates=())


def _audit(freeze_version: str, freeze_hash: str) -> CommittedResearchAudit:
    return _point_in_time_audit(
        freeze_version,
        freeze_hash,
        datetime(2026, 8, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )


def _legacy_audit(freeze_version: str, freeze_hash: str) -> CommittedResearchAudit:
    freeze_set = ResearchFreezeSetAudit(
        freeze_version=freeze_version,
        candidates=(
            ResearchFreezeCandidateAudit(
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
    return CommittedResearchAudit(
        freeze_version=freeze_version,
        freeze_hash=freeze_hash,
        input_version="native-input",
        hard_filter_aggregates=(("main:st_or_delisting", 2),),
        passed_candidates=(
            ResearchCandidateAudit(
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
        production_local=freeze_set,
        research_shadow=freeze_set,
        shadow_mode="control_copy",
        schema_version=LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    )


def _point_in_time_audit(freeze_version: str, freeze_hash: str, observed_at: datetime) -> CommittedResearchAudit:
    legacy = _legacy_audit(freeze_version, freeze_hash)
    population = (
        ResearchPopulationAudit(
            code="600001",
            board="main",
            industry="equipment",
            feature_observed_at=observed_at,
            quote_source_time=observed_at,
            quote_source="tencent",
            data_version="market-input",
            is_st=False,
            listing_date=date(2010, 1, 1),
            is_relisted_first_session=False,
            is_delisting_period_first_session=False,
            has_delisting_name=False,
            structured_risk_values=tuple(
                (name, None)
                for name in (
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
            ),
            external_risk_facts=(),
            filter_reasons=(),
            disposition="candidate",
            requested_for_refresh=True,
        ),
    )
    return CommittedResearchAudit(
        freeze_version=legacy.freeze_version,
        freeze_hash=legacy.freeze_hash,
        input_version=legacy.input_version,
        hard_filter_aggregates=legacy.hard_filter_aggregates,
        passed_candidates=legacy.passed_candidates,
        production_local=legacy.production_local,
        research_shadow=legacy.research_shadow,
        shadow_mode=legacy.shadow_mode,
        input_observed_at=observed_at,
        point_in_time_population=population,
        point_in_time_population_hash=PointInTimePopulationHash(population),
    )
