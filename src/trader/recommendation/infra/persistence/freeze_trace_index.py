"""Recommendation-owned freeze trace index for immutable freeze observations."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, cast

from trader.infra.process_lock import ProcessLock
from trader.infra.serialization.research_trace_validation import (
    Boolean as _boolean,
)
from trader.infra.serialization.research_trace_validation import (
    CountPairs as _count_pairs,
)
from trader.infra.serialization.research_trace_validation import (
    Integer as _integer,
)
from trader.infra.serialization.research_trace_validation import (
    ListValue as _list,
)
from trader.infra.serialization.research_trace_validation import (
    Number as _number,
)
from trader.infra.serialization.research_trace_validation import (
    ObjectValue as _object,
)
from trader.infra.serialization.research_trace_validation import (
    OptionalBoolean as _optional_boolean,
)
from trader.infra.serialization.research_trace_validation import (
    OptionalNumber as _optional_number,
)
from trader.infra.serialization.research_trace_validation import (
    OptionalNumberPairs as _optional_number_pairs,
)
from trader.infra.serialization.research_trace_validation import (
    OptionalText as _optional_text,
)
from trader.infra.serialization.research_trace_validation import (
    RequiredScorePairs as _required_score_pairs,
)
from trader.infra.serialization.research_trace_validation import (
    ScorePairs as _score_pairs,
)
from trader.infra.serialization.research_trace_validation import (
    Strings as _strings,
)
from trader.infra.serialization.research_trace_validation import (
    Text as _text,
)
from trader.infra.serialization.research_trace_validation import (
    TextPairs as _text_pairs,
)
from trader.infra.sqlite_codec import DecodeDate, DecodeHash, DecodeTime, EncodeDate, EncodeHash, EncodeTime
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import (
    CommittedFreezeItem,
    FreezeCommitted,
    FreezeObservation,
)
from trader.recommendation.application.ports.read_only_queries import ResearchAuditIdentity
from trader.recommendation.domain.publication.freeze_identity import FreezeStage, IsPreviewFreeze
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.infra.persistence.trace_capacity import ResearchTraceCapacityError, TracePhysicalBudget
from trader.recommendation.infra.persistence.trace_codec import (
    DEFAULT_DECODED_PAYLOAD_BYTES,
    DecodedPayloadBytes,
    PersistedPayloadBytes,
    Sha256,
)
from trader.recommendation.infra.persistence.trace_projection import ObservationBytes
from trader.recommendation.infra.persistence.trace_schema import (
    SCHEMA,
    InitializeDatabase,
    PartitionDate,
    QuarantineRow,
    TraceIndexSummary,
)
from trader.training.application.research_audit import (
    LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    RESEARCH_AUDIT_SCHEMA_VERSION,
    CommittedResearchAudit,
    ResearchCandidateAudit,
    ResearchFreezeCandidateAudit,
    ResearchFreezeSetAudit,
    ResearchPopulationAudit,
    ResearchRiskFactAudit,
    ShadowMode,
)

LEGACY_RESEARCH_EVENT_SCHEMA_VERSION = "research_committed_event_legacy"
RESEARCH_EVENT_SCHEMA_VERSION = "research_committed_event"
_SCHEMA_VERSION = RESEARCH_EVENT_SCHEMA_VERSION


class ResearchTraceConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchTraceStatus:
    retained: int
    retained_bytes: int
    recorded: int
    duplicate: int
    quarantined: int
    trade_dates: int
    trade_date_capacity: int
    legacy_retained: int
    total_capacity_bytes: int
    remaining_bytes: int


@dataclass(frozen=True)
class ResearchTradeDateObservation:
    trade_date: date
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("research trace observation must be timezone-aware")
        if self.observed_at.date() != self.trade_date:
            raise ValueError("research trace observation must match its trade date")


@dataclass(frozen=True)
class ResearchTraceLimits:
    events_per_trade_date: int = 4096
    payload_bytes: int = 4 * 1024 * 1024
    decoded_payload_bytes: int = DEFAULT_DECODED_PAYLOAD_BYTES
    trade_date_bytes: int = 1024 * 1024 * 1024
    total_bytes: int = 20 * 1024 * 1024 * 1024
    trade_dates: int = 120
    physical_trade_date_bytes: int = 1024 * 1024 * 1024
    physical_total_bytes: int = 20 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.events_per_trade_date,
            self.payload_bytes,
            self.decoded_payload_bytes,
            self.trade_date_bytes,
            self.total_bytes,
            self.trade_dates,
            self.physical_trade_date_bytes,
            self.physical_total_bytes,
        )
        if min(values) < 1:
            raise ValueError("research trace capacities must be positive")
        if self.payload_bytes > self.trade_date_bytes or self.trade_date_bytes > self.total_bytes:
            raise ValueError("research payload capacity cannot exceed total capacity")


def _require_persistable_observation(observation: FreezeObservation) -> None:
    event = observation.event
    if ("run_mode", "preview") in event.input_versions or (
        event.projection is not None and IsPreviewFreeze(event.projection)
    ):
        raise ValueError("preview freezes cannot enter research traces")


class SQLiteFreezeTraceIndex:
    """Observer consumer that persists only the generic committed event."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        limits: ResearchTraceLimits | None = None,
    ) -> None:
        limits = limits or ResearchTraceLimits()
        self._root = runtime_root
        self._writer_lock = ProcessLock(self._root / ".trace-write.lock")
        self._partitions = self._root / "traces"
        self._capacity = limits.events_per_trade_date
        self._maximum_payload_bytes = limits.payload_bytes
        self._maximum_decoded_payload_bytes = limits.decoded_payload_bytes
        self._maximum_partition_bytes = limits.trade_date_bytes
        self._maximum_total_bytes = limits.total_bytes
        self._maximum_trade_dates = limits.trade_dates
        self._physical_budget = TracePhysicalBudget(limits.physical_trade_date_bytes, limits.physical_total_bytes)
        self._lock = threading.RLock()
        self._initialized_databases: set[Path] = set()
        self._recorded = 0
        self._duplicate = 0
        self._quarantined = 0
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._partitions.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def record(self, observation: FreezeObservation) -> None:
        _require_persistable_observation(observation)
        self.initialize()
        observation = observation.materialize()
        event = observation.event
        if event.freeze_hash is None:
            raise ValueError("freeze event requires a materialized hash")
        if (
            observation.research_audit is not None
            and observation.research_audit.schema_version != RESEARCH_AUDIT_SCHEMA_VERSION
        ):
            raise ValueError("legacy research audit is read-only")
        logical_payload = ObservationBytes(
            observation,
            schema_version=RESEARCH_EVENT_SCHEMA_VERSION,
            legacy_schema_version=LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
            current_schema_version=RESEARCH_EVENT_SCHEMA_VERSION,
        )
        if len(logical_payload) > self._maximum_decoded_payload_bytes:
            raise ResearchTraceCapacityError("research trace decoded payload capacity exhausted")
        payload = PersistedPayloadBytes(logical_payload)
        payload_hash = Sha256(logical_payload)
        if len(payload) > self._maximum_payload_bytes:
            raise ResearchTraceCapacityError("research trace payload capacity exhausted")
        database = self._database_for(event.trade_date)
        with self._lock, self._writer_lock:
            existing = self._existing_row(event.freeze_version, event.trade_date, database)
            if existing is not None:
                existing_payload = bytes(existing["event_bytes"])
                if (
                    DecodeHash(existing["event_hash"]) == payload_hash
                    and DecodedPayloadBytes(
                        existing_payload,
                        maximum_bytes=self._maximum_decoded_payload_bytes,
                    )
                    == logical_payload
                ):
                    self._duplicate += 1
                    return
                persisted = _observation_from_bytes(
                    existing_payload,
                    DecodeHash(existing["event_hash"]),
                    maximum_decoded_bytes=self._maximum_decoded_payload_bytes,
                )
                if persisted.event == event and observation.research_audit is None:
                    self._duplicate += 1
                    return
                raise ResearchTraceConflictError("research trace identity conflict")
            summary = self._trace_index_summary()
            if event.trade_date not in summary.trade_dates and len(summary.trade_dates) >= self._maximum_trade_dates:
                raise ResearchTraceCapacityError("research trace trade-date capacity exhausted")
            if summary.retained_bytes + len(payload) > self._maximum_total_bytes:
                raise ResearchTraceCapacityError("research trace total capacity exhausted")
            self._physical_budget.require(self._source_databases(), database, pending_bytes=65536)
            self._initialize_database(database)
            with SqliteSession(database) as connection, SqliteTransaction(connection, immediate=True):
                connection.run("PRAGMA cache_spill = OFF")
                retained = int(connection.run("SELECT COUNT(*) FROM committed_events").require_one()[0])
                retained_bytes = int(
                    connection.run("SELECT COALESCE(SUM(LENGTH(event_bytes)), 0) FROM committed_events").require_one()[
                        0
                    ]
                )
                if retained >= self._capacity or retained_bytes + len(payload) > self._maximum_partition_bytes:
                    raise ResearchTraceCapacityError("research trace partition capacity exhausted")
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
                        _SCHEMA_VERSION,
                        EncodeHash(payload_hash),
                        payload,
                    ),
                )
                self._physical_budget.require_commit(connection, self._source_databases(), database)
            self._recorded += 1

    def get(self, freeze_version: str) -> FreezeObservation | None:
        with self._lock:
            for database in self._source_databases():
                with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
                    EnsureSqliteSchema(connection, SCHEMA)
                    row = connection.run(
                        "SELECT * FROM committed_events WHERE freeze_id = ?",
                        (freeze_version,),
                    ).fetchone()
                if row is None:
                    continue
                try:
                    return _verified_event(row, maximum_decoded_bytes=self._maximum_decoded_payload_bytes)
                except (TypeError, ValueError) as exc:
                    raise ResearchTraceConflictError("research trace verification failed") from exc
        return None

    def list_trade_dates(self, *, limit: int = 40) -> tuple[date, ...]:
        if limit < 1:
            raise ValueError("research trace date limit must be positive")
        return self.inspect_trade_dates(limit=limit)

    def inspect_trade_dates(self, *, limit: int = 40) -> tuple[date, ...]:
        if limit < 1:
            raise ValueError("research trace date limit must be positive")
        dates: set[date] = set()
        with self._lock:
            for database in self._source_databases():
                with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
                    EnsureSqliteSchema(connection, SCHEMA)
                    rows = connection.run("SELECT DISTINCT trade_date FROM committed_events").fetchall()
                dates.update(DecodeDate(row["trade_date"]) for row in rows)
        return tuple(sorted(dates, reverse=True)[:limit])

    def inspect_first_observations(self, *, limit: int = 40) -> tuple[ResearchTradeDateObservation, ...]:
        """Read the earliest committed observation for each retained trade date."""

        if limit < 1:
            raise ValueError("research trace date limit must be positive")
        earliest: dict[date, datetime] = {}
        with self._lock:
            for database in self._source_databases():
                with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
                    EnsureSqliteSchema(connection, SCHEMA)
                    rows = connection.run(
                        """
                        SELECT trade_date, MIN(observed_at) AS observed_at
                        FROM committed_events
                        GROUP BY trade_date
                        """
                    ).fetchall()
                for row in rows:
                    trade_date = DecodeDate(row["trade_date"])
                    observed_at = DecodeTime(row["observed_at"])
                    observation = ResearchTradeDateObservation(trade_date, observed_at)
                    current = earliest.get(trade_date)
                    if current is None or observation.observed_at < current:
                        earliest[trade_date] = observation.observed_at
        return tuple(
            ResearchTradeDateObservation(trade_date, earliest[trade_date])
            for trade_date in sorted(earliest, reverse=True)[:limit]
        )

    def list_by_trade_date(self, trade_date: date) -> tuple[FreezeObservation, ...]:
        observations: dict[str, FreezeObservation] = {}
        with self._lock:
            for database in self._source_databases(trade_date):
                with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
                    EnsureSqliteSchema(connection, SCHEMA)
                    rows = connection.run(
                        "SELECT * FROM committed_events WHERE trade_date = ?",
                        (EncodeDate(trade_date),),
                    ).fetchall()
                for row in rows:
                    version = str(row["freeze_id"])
                    try:
                        observation = _verified_event(row, maximum_decoded_bytes=self._maximum_decoded_payload_bytes)
                    except (TypeError, ValueError) as exc:
                        raise ResearchTraceConflictError("research trace verification failed") from exc
                    current = observations.get(version)
                    if current is not None and current != observation:
                        raise ResearchTraceConflictError("research trace cross-partition identity conflict")
                    observations[version] = observation
        return tuple(
            sorted(
                observations.values(),
                key=lambda item: (item.event.observed_at, item.event.strategy.value, item.event.freeze_version),
            )
        )

    def status(self) -> ResearchTraceStatus:
        return self.inspect_status()

    def inspect_status(self) -> ResearchTraceStatus:
        with self._lock:
            summary = self._trace_index_summary()
            return ResearchTraceStatus(
                summary.retained,
                summary.retained_bytes,
                self._recorded,
                self._duplicate,
                self._quarantined,
                len(summary.trade_dates),
                self._maximum_trade_dates,
                summary.legacy_retained,
                self._maximum_total_bytes,
                max(0, self._maximum_total_bytes - summary.retained_bytes),
            )

    def _database_for(self, trade_date: date) -> Path:
        return self._partitions / f"{trade_date.isoformat()}.sqlite3"

    def _initialize_database(self, database: Path) -> None:
        if database in self._initialized_databases:
            return
        self._physical_budget.require(self._source_databases(), database, pending_bytes=262144)
        InitializeDatabase(database, self._initialized_databases)
        with SqliteSession(database) as connection, SqliteTransaction(connection, immediate=True):
            connection.run("PRAGMA cache_spill = OFF")
            quarantined = self._quarantine_invalid_rows(connection)
            if quarantined:
                self._physical_budget.require_commit(connection, self._source_databases(), database)
        self._quarantined += quarantined

    def _partition_databases(self) -> tuple[Path, ...]:
        if not self._partitions.exists():
            return ()
        return tuple(path for path in sorted(self._partitions.glob("*.sqlite3")) if PartitionDate(path) is not None)

    def _source_databases(self, trade_date: date | None = None) -> tuple[Path, ...]:
        databases: list[Path] = []
        if trade_date is None:
            databases.extend(self._partition_databases())
        else:
            partition = self._partitions / f"{trade_date.isoformat()}.sqlite3"
            if partition.is_file():
                databases.append(partition)
        return tuple(databases)

    def _existing_row(self, freeze_version: str, trade_date: date, database: Path) -> sqlite3.Row | None:
        for source in dict.fromkeys((*self._source_databases(trade_date), database)):
            if not source.is_file():
                continue
            with SqliteSession(source, SqliteConnectionOptions(read_only=True)) as connection:
                EnsureSqliteSchema(connection, SCHEMA)
                row = connection.run(
                    "SELECT event_hash, event_bytes FROM committed_events WHERE freeze_id = ?",
                    (freeze_version,),
                ).fetchone()
            if row is not None:
                return row
        return None

    def _trace_index_summary(self) -> TraceIndexSummary:
        retained = 0
        retained_bytes = 0
        legacy_retained = 0
        dates: set[date] = set()
        for database in self._source_databases():
            with SqliteSession(database, SqliteConnectionOptions(read_only=True)) as connection:
                EnsureSqliteSchema(connection, SCHEMA)
                row = connection.run(
                    "SELECT COUNT(*) retained, COALESCE(SUM(LENGTH(event_bytes)), 0) retained_bytes FROM committed_events"
                ).require_one()
                day_rows = connection.run("SELECT DISTINCT trade_date FROM committed_events").fetchall()
            count = int(row["retained"])
            retained += count
            retained_bytes += int(row["retained_bytes"])
            dates.update(DecodeDate(item["trade_date"]) for item in day_rows)
        return TraceIndexSummary(retained, retained_bytes, frozenset(dates), legacy_retained)

    def _quarantine_invalid_rows(self, connection: SqliteConnection) -> int:
        quarantined = 0
        rows = connection.run("SELECT * FROM committed_events").fetchall()
        for row in rows:
            try:
                observation = _verified_event(row, maximum_decoded_bytes=self._maximum_decoded_payload_bytes)
                if observation.event.freeze_version != str(row["freeze_id"]):
                    raise ValueError("research event identity mismatch")
            except (TypeError, ValueError, json.JSONDecodeError):
                self._quarantine_row(connection, str(row["freeze_id"]), "startup_verification_failed")
                quarantined += 1
        return quarantined

    def _quarantine_row(self, connection: SqliteConnection, freeze_version: str, reason: str) -> None:
        QuarantineRow(connection, freeze_version, reason)


def _verified_event(
    row: sqlite3.Row,
    *,
    maximum_decoded_bytes: int = DEFAULT_DECODED_PAYLOAD_BYTES,
) -> FreezeObservation:
    persisted_payload = bytes(row["event_bytes"])
    payload = DecodedPayloadBytes(persisted_payload, maximum_bytes=maximum_decoded_bytes)
    row_schema = str(row["schema_version"])
    if row_schema not in {LEGACY_RESEARCH_EVENT_SCHEMA_VERSION, RESEARCH_EVENT_SCHEMA_VERSION}:
        raise ValueError("research event row schema is invalid")
    raw = json.loads(payload)
    if not isinstance(raw, dict) or raw.get("schema_version") != row_schema:
        raise ValueError("research event row and payload schema mismatch")
    observation = _observation_from_bytes(
        persisted_payload,
        DecodeHash(row["event_hash"]),
        maximum_decoded_bytes=maximum_decoded_bytes,
    )
    event = observation.event
    if (
        event.freeze_version != str(row["freeze_id"])
        or event.strategy.value != str(row["strategy"])
        or EncodeDate(event.trade_date) != row["trade_date"]
        or EncodeTime(event.observed_at) != row["observed_at"]
        or event.freeze_hash != DecodeHash(row["freeze_hash"])
    ):
        raise ValueError("research event row identity mismatch")
    return observation


def _observation_from_bytes(
    persisted_payload: bytes,
    payload_hash: str,
    *,
    maximum_decoded_bytes: int = DEFAULT_DECODED_PAYLOAD_BYTES,
) -> FreezeObservation:
    payload = DecodedPayloadBytes(persisted_payload, maximum_bytes=maximum_decoded_bytes)
    if Sha256(payload) != payload_hash:
        raise ValueError("research event payload hash mismatch")
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("research event payload must be an object")
    return _observation_from_dict(cast(dict[str, object], raw))


def _observation_from_dict(raw: dict[str, object]) -> FreezeObservation:
    schema_version = raw.get("schema_version")
    if schema_version not in {
        LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
        RESEARCH_EVENT_SCHEMA_VERSION,
    }:
        raise ValueError("research event schema is invalid")
    stage = _text(raw, "stage")
    if stage not in {"local", "hybrid"}:
        raise ValueError("research event stage is invalid")
    event = FreezeCommitted(
        event_id=_text(raw, "event_id"),
        strategy=Strategy(_text(raw, "strategy")),
        trade_date=date.fromisoformat(_text(raw, "trade_date")),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        freeze_version=_text(raw, "freeze_version"),
        freeze_hash=_text(raw, "freeze_hash"),
        parent_version=_optional_text(raw.get("parent_version")),
        stage=cast(FreezeStage, stage),
        input_versions=_text_pairs(raw.get("input_versions"), "input_versions"),
        config_version=_text(raw, "config_version"),
        strategy_version=_text(raw, "strategy_version"),
        fusion_version=_text(raw, "fusion_version"),
        schema_version=_text(raw, "freeze_schema_version"),
        filter_aggregates=_count_pairs(raw.get("filter_aggregates")),
        degraded_reasons=tuple(_strings(raw.get("degraded_reasons"), "degraded_reasons")),
        items=tuple(_item(_object(item, "item")) for item in _list(raw.get("items"), "items")),
    )
    audit_raw = raw.get("research_audit")
    audit = None if audit_raw is None else _audit(_object(audit_raw, "research_audit"))
    expected_audit_schema = (
        LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION
        if schema_version == LEGACY_RESEARCH_EVENT_SCHEMA_VERSION
        else RESEARCH_AUDIT_SCHEMA_VERSION
    )
    if audit is not None and audit.schema_version != expected_audit_schema:
        raise ValueError("research event and audit schemas must advance together")
    return FreezeObservation(event, cast(ResearchAuditIdentity | None, audit))


def _item(raw: dict[str, object]) -> CommittedFreezeItem:
    return CommittedFreezeItem(
        code=_text(raw, "code"),
        action=RecommendationAction(_text(raw, "action")),
        selected=_boolean(raw, "selected"),
        rank=_integer(raw, "rank"),
        candidate_score=_optional_number(raw.get("candidate_score")),
        local_score=_number(raw, "local_score"),
        final_score=_number(raw, "final_score"),
        score_components=_score_pairs(raw.get("score_components")),
        risk_codes=tuple(_strings(raw.get("risk_codes"), "risk_codes")),
        reason=_text(raw, "reason"),
    )


def _audit(raw: dict[str, object]) -> CommittedResearchAudit:
    schema_version = _text(raw, "schema_version")
    is_current_schema = schema_version == RESEARCH_AUDIT_SCHEMA_VERSION
    if schema_version not in {LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION, RESEARCH_AUDIT_SCHEMA_VERSION}:
        raise ValueError("research audit schema is invalid")
    audit = CommittedResearchAudit(
        freeze_version=_text(raw, "freeze_version"),
        freeze_hash=_text(raw, "freeze_hash"),
        input_version=_text(raw, "input_version"),
        hard_filter_aggregates=_count_pairs(raw.get("hard_filter_aggregates")),
        passed_candidates=tuple(
            _candidate_audit(_object(item, "passed_candidate"))
            for item in _list(raw.get("passed_candidates"), "passed_candidates")
        ),
        production_local=_freeze_set_audit(_object(raw.get("production_local"), "production_local")),
        research_shadow=_freeze_set_audit(_object(raw.get("research_shadow"), "research_shadow")),
        shadow_mode=cast(ShadowMode, _text(raw, "shadow_mode")),
        deepseek_request_delta=_integer(raw, "deepseek_request_delta"),
        schema_version=schema_version,
        input_observed_at=(datetime.fromisoformat(_text(raw, "input_observed_at")) if is_current_schema else None),
        point_in_time_population=(
            tuple(
                _population_audit(_object(item, "point_in_time_population"))
                for item in _list(raw.get("point_in_time_population"), "point_in_time_population")
            )
            if is_current_schema
            else ()
        ),
        point_in_time_population_hash=(_text(raw, "point_in_time_population_hash") if is_current_schema else ""),
    )
    if audit.content_hash != _text(raw, "content_hash"):
        raise ValueError("research audit content hash mismatch")
    return audit


def _population_audit(raw: dict[str, object]) -> ResearchPopulationAudit:
    listing_date_raw = raw.get("listing_date")
    return ResearchPopulationAudit(
        code=_text(raw, "code"),
        board=_text(raw, "board"),
        industry=_text(raw, "industry"),
        feature_observed_at=datetime.fromisoformat(_text(raw, "feature_observed_at")),
        quote_source_time=datetime.fromisoformat(_text(raw, "quote_source_time")),
        quote_source=_text(raw, "quote_source"),
        data_version=_text(raw, "data_version"),
        is_st=_boolean(raw, "is_st"),
        listing_date=(date.fromisoformat(listing_date_raw) if isinstance(listing_date_raw, str) else None),
        is_relisted_first_session=_optional_boolean(raw.get("is_relisted_first_session")),
        is_delisting_period_first_session=_optional_boolean(raw.get("is_delisting_period_first_session")),
        has_delisting_name=_boolean(raw, "has_delisting_name"),
        structured_risk_values=_optional_number_pairs(raw.get("structured_risk_values"), "structured_risk_values"),
        external_risk_facts=tuple(
            _risk_fact_audit(_object(item, "external_risk_fact"))
            for item in _list(raw.get("external_risk_facts"), "external_risk_facts")
        ),
        filter_reasons=tuple(_strings(raw.get("filter_reasons"), "filter_reasons")),
        disposition=_text(raw, "disposition"),
        requested_for_refresh=_boolean(raw, "requested_for_refresh"),
    )


def _risk_fact_audit(raw: dict[str, object]) -> ResearchRiskFactAudit:
    return ResearchRiskFactAudit(
        risk_code=_text(raw, "risk_code"),
        source=_text(raw, "source"),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        confidence=_number(raw, "confidence"),
        veto=_boolean(raw, "veto"),
    )


def _candidate_audit(raw: dict[str, object]) -> ResearchCandidateAudit:
    return ResearchCandidateAudit(
        code=_text(raw, "code"),
        board=_text(raw, "board"),
        industry=_text(raw, "industry"),
        candidate_components=_required_score_pairs(raw.get("candidate_components")),
        missing_mask=tuple(_strings(raw.get("missing_mask"), "missing_mask")),
        coverage_ratio=_number(raw, "coverage_ratio"),
        board_reliability=_number(raw, "board_reliability"),
        candidate_score=_optional_number(raw.get("candidate_score")),
        candidate_rank=_integer(raw, "candidate_rank"),
        production_top120=_boolean(raw, "production_top120"),
        full_population_scored=_boolean(raw, "full_population_scored") if "full_population_scored" in raw else None,
        preselection_status=_text(raw, "preselection_status"),
        optimistic_upper_bound=_optional_number(raw.get("optimistic_upper_bound")),
        upper_bound_status=cast(Literal["not_computed"], _text(raw, "upper_bound_status")),
        upper_bound_protected=_boolean(raw, "upper_bound_protected"),
    )


def _freeze_set_audit(raw: dict[str, object]) -> ResearchFreezeSetAudit:
    return ResearchFreezeSetAudit(
        freeze_version=_text(raw, "freeze_version"),
        candidates=tuple(
            _freeze_candidate_audit(_object(item, "freeze_candidate"))
            for item in _list(raw.get("candidates"), "freeze_candidates")
        ),
    )


def _freeze_candidate_audit(raw: dict[str, object]) -> ResearchFreezeCandidateAudit:
    return ResearchFreezeCandidateAudit(
        code=_text(raw, "code"),
        components=_score_pairs(raw.get("components")),
        component_coverage_ratio=_number(raw, "component_coverage_ratio"),
        base_score=_number(raw, "base_score"),
        local_risk_codes=tuple(_strings(raw.get("local_risk_codes"), "local_risk_codes")),
        local_risk_penalty=_number(raw, "local_risk_penalty"),
        local_score=_number(raw, "local_score"),
        reused_deepseek_facts=_boolean(raw, "reused_deepseek_facts"),
        fusion_applied=_boolean(raw, "fusion_applied"),
        deepseek_risk_codes=tuple(_strings(raw.get("deepseek_risk_codes"), "deepseek_risk_codes")),
        deepseek_risk_penalty=_number(raw, "deepseek_risk_penalty"),
        final_score=_number(raw, "final_score"),
        action=_text(raw, "action"),
        selected=_boolean(raw, "selected"),
        rank=_integer(raw, "rank"),
        board_rank=_integer(raw, "board_rank"),
        skip_reason=_text(raw, "skip_reason"),
    )


__all__ = [
    "LEGACY_RESEARCH_EVENT_SCHEMA_VERSION",
    "RESEARCH_EVENT_SCHEMA_VERSION",
    "ResearchTraceConflictError",
    "ResearchTraceLimits",
    "SQLiteFreezeTraceIndex",
    "ResearchTradeDateObservation",
    "ResearchTraceStatus",
]
