"""Immutable SQLite audit of committed V2 decision events for research."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo

from trader.application.decisions.decision_events import (
    V2CommittedDecisionItem,
    V2DecisionCommitted,
)
from trader.application.research_audit import (
    LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION,
    RESEARCH_AUDIT_SCHEMA_VERSION,
    ShadowMode,
    V2CommittedResearchAudit,
    V2DecisionObservation,
    V2ResearchCandidateAudit,
    V2ResearchDecisionCandidateAudit,
    V2ResearchDecisionSetAudit,
    V2ResearchPopulationAudit,
    V2ResearchRiskFactAudit,
)
from trader.domain.recommendation.decision_identity import DecisionStage
from trader.domain.recommendation.models import RecommendationAction, Strategy

LEGACY_RESEARCH_EVENT_SCHEMA_VERSION = "v2_research_committed_event_v1"
RESEARCH_EVENT_SCHEMA_VERSION = "v2_research_committed_event_v2"
_SCHEMA_VERSION = RESEARCH_EVENT_SCHEMA_VERSION
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchTraceConflictError(RuntimeError):
    pass


class ResearchTraceCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class V2ResearchTraceStatus:
    retained: int
    retained_bytes: int
    recorded: int
    duplicate: int
    quarantined: int
    trade_dates: int
    trade_date_capacity: int
    legacy_retained: int
    archive_capacity_bytes: int
    remaining_bytes: int


@dataclass(frozen=True)
class V2ResearchTradeDateObservation:
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
    trade_date_bytes: int = 1024 * 1024 * 1024
    archive_bytes: int = 20 * 1024 * 1024 * 1024
    trade_dates: int = 120

    def __post_init__(self) -> None:
        values = (
            self.events_per_trade_date,
            self.payload_bytes,
            self.trade_date_bytes,
            self.archive_bytes,
            self.trade_dates,
        )
        if min(values) < 1:
            raise ValueError("V2 research trace capacities must be positive")
        if self.payload_bytes > self.trade_date_bytes or self.trade_date_bytes > self.archive_bytes:
            raise ValueError("V2 research payload capacity cannot exceed total capacity")


class SQLiteV2ResearchTraceStore:
    """Observer consumer that persists only the generic committed event."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        limits: ResearchTraceLimits | None = None,
        use_legacy_layout: bool = False,
    ) -> None:
        limits = limits or ResearchTraceLimits()
        self._root = runtime_root / "research"
        self._legacy_database = self._root / "committed-events.sqlite3"
        self._partitions = self._root / "committed-events"
        self._capacity = limits.events_per_trade_date
        self._maximum_payload_bytes = limits.payload_bytes
        self._maximum_total_bytes = limits.trade_date_bytes
        self._maximum_archive_bytes = limits.archive_bytes
        self._maximum_trade_dates = limits.trade_dates
        self._use_legacy_layout = use_legacy_layout
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
            if self._use_legacy_layout:
                self._initialize_database(self._legacy_database)
            else:
                self._partitions.mkdir(parents=True, exist_ok=True)
            self._initialized = True

    def record(self, observation: V2DecisionObservation) -> None:
        self.initialize()
        event = observation.event
        if (
            observation.research_audit is not None
            and observation.research_audit.schema_version != RESEARCH_AUDIT_SCHEMA_VERSION
        ):
            raise ValueError("legacy research audit is read-only")
        payload = _observation_bytes(observation)
        payload_hash = _sha256(payload)
        if len(payload) > self._maximum_payload_bytes:
            raise ResearchTraceCapacityError("V2 research trace payload capacity exhausted")
        database = self._database_for(event.trade_date)
        with self._lock:
            existing = self._existing_row(event.decision_version, event.trade_date, database)
            if existing is not None:
                if str(existing["payload_hash"]) == payload_hash and bytes(existing["payload"]) == payload:
                    self._duplicate += 1
                    return
                stored = _observation_from_bytes(bytes(existing["payload"]), str(existing["payload_hash"]))
                if stored.event == event and observation.research_audit is None:
                    self._duplicate += 1
                    return
                raise ResearchTraceConflictError("V2 research trace identity conflict")
            archive = self._archive_summary()
            if event.trade_date not in archive.trade_dates and len(archive.trade_dates) >= self._maximum_trade_dates:
                raise ResearchTraceCapacityError("V2 research trace trade-date capacity exhausted")
            if archive.retained_bytes + len(payload) > self._maximum_archive_bytes:
                raise ResearchTraceCapacityError("V2 research trace archive capacity exhausted")
            self._initialize_database(database)
            with self._connection(database, write=True) as connection:
                retained = int(connection.execute("SELECT COUNT(*) FROM committed_events").fetchone()[0])
                retained_bytes = int(
                    connection.execute("SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM committed_events").fetchone()[0]
                )
                if retained >= self._capacity or retained_bytes + len(payload) > self._maximum_total_bytes:
                    raise ResearchTraceCapacityError("V2 research trace partition capacity exhausted")
                connection.execute(
                    """
                    INSERT INTO committed_events (
                        decision_version, strategy, trade_date, observed_at,
                        decision_hash, schema_version, payload_hash, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.decision_version,
                        event.strategy.value,
                        event.trade_date.isoformat(),
                        event.observed_at.isoformat(),
                        event.decision_hash,
                        _SCHEMA_VERSION,
                        payload_hash,
                        payload,
                    ),
                )
                self._recorded += 1

    def get(self, decision_version: str) -> V2DecisionObservation | None:
        self.initialize()
        with self._lock:
            for database in self._source_databases():
                with self._connection(database, write=False) as connection:
                    row = connection.execute(
                        "SELECT * FROM committed_events WHERE decision_version = ?",
                        (decision_version,),
                    ).fetchone()
                if row is None:
                    continue
                try:
                    return _verified_event(row)
                except (TypeError, ValueError, json.JSONDecodeError):
                    self._quarantine_database_row(database, decision_version, "verification_failed")
                    return None
        return None

    def list_trade_dates(self, *, limit: int = 40) -> tuple[date, ...]:
        if limit < 1:
            raise ValueError("research trace date limit must be positive")
        self.initialize()
        return self.inspect_trade_dates(limit=limit)

    def inspect_trade_dates(self, *, limit: int = 40) -> tuple[date, ...]:
        if limit < 1:
            raise ValueError("research trace date limit must be positive")
        dates: set[date] = set()
        with self._lock:
            for database in self._source_databases():
                with self._connection(database, write=False) as connection:
                    rows = connection.execute("SELECT DISTINCT trade_date FROM committed_events").fetchall()
                dates.update(date.fromisoformat(str(row["trade_date"])) for row in rows)
        return tuple(sorted(dates, reverse=True)[:limit])

    def inspect_first_observations(self, *, limit: int = 40) -> tuple[V2ResearchTradeDateObservation, ...]:
        """Read the earliest committed observation for each retained trade date."""

        if limit < 1:
            raise ValueError("research trace date limit must be positive")
        earliest: dict[date, datetime] = {}
        with self._lock:
            for database in self._source_databases():
                with self._connection(database, write=False) as connection:
                    rows = connection.execute(
                        """
                        SELECT trade_date, MIN(observed_at) AS observed_at
                        FROM committed_events
                        GROUP BY trade_date
                        """
                    ).fetchall()
                for row in rows:
                    trade_date = date.fromisoformat(str(row["trade_date"]))
                    observed_at = datetime.fromisoformat(str(row["observed_at"]))
                    observation = V2ResearchTradeDateObservation(trade_date, observed_at)
                    current = earliest.get(trade_date)
                    if current is None or observation.observed_at < current:
                        earliest[trade_date] = observation.observed_at
        return tuple(
            V2ResearchTradeDateObservation(trade_date, earliest[trade_date])
            for trade_date in sorted(earliest, reverse=True)[:limit]
        )

    def list_by_trade_date(self, trade_date: date) -> tuple[V2DecisionObservation, ...]:
        self.initialize()
        observations: dict[str, V2DecisionObservation] = {}
        with self._lock:
            for database in self._source_databases(trade_date):
                with self._connection(database, write=False) as connection:
                    rows = connection.execute(
                        "SELECT * FROM committed_events WHERE trade_date = ?",
                        (trade_date.isoformat(),),
                    ).fetchall()
                for row in rows:
                    version = str(row["decision_version"])
                    try:
                        observation = _verified_event(row)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        self._quarantine_database_row(database, version, "verification_failed")
                        continue
                    current = observations.get(version)
                    if current is not None and current != observation:
                        raise ResearchTraceConflictError("V2 research trace cross-partition identity conflict")
                    observations[version] = observation
        return tuple(
            sorted(
                observations.values(),
                key=lambda item: (item.event.observed_at, item.event.strategy.value, item.event.decision_version),
            )
        )

    def latest_point_in_time_observation(
        self,
        trade_date: date,
        strategy: Strategy,
        *,
        cutoff: time = time(hour=14, minute=50),
    ) -> V2DecisionObservation | None:
        """Return the latest complete local input that existed by the research cutoff."""

        if cutoff.tzinfo is not None:
            raise ValueError("research cutoff must be a Shanghai wall-clock time")
        cutoff_at = datetime.combine(trade_date, cutoff, tzinfo=_SHANGHAI)
        eligible: list[V2DecisionObservation] = []
        for observation in self.list_by_trade_date(trade_date):
            audit = observation.research_audit
            if (
                observation.event.strategy is not strategy
                or observation.event.observed_at.astimezone(_SHANGHAI) > cutoff_at
                or audit is None
                or audit.schema_version != RESEARCH_AUDIT_SCHEMA_VERSION
                or audit.shadow_mode != "control_copy"
                or audit.input_observed_at is None
                or audit.input_observed_at.astimezone(_SHANGHAI) > cutoff_at
                or not audit.point_in_time_population
            ):
                continue
            if (
                observation.event.observed_at.astimezone(_SHANGHAI).date() != trade_date
                or audit.input_observed_at.astimezone(_SHANGHAI).date() != trade_date
            ):
                continue
            eligible.append(observation)
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda item: (
                cast(V2CommittedResearchAudit, item.research_audit).input_observed_at,
                item.event.observed_at,
                item.event.decision_version,
            ),
        )

    def status(self) -> V2ResearchTraceStatus:
        self.initialize()
        return self.inspect_status()

    def inspect_status(self) -> V2ResearchTraceStatus:
        with self._lock:
            summary = self._archive_summary()
            return V2ResearchTraceStatus(
                summary.retained,
                summary.retained_bytes,
                self._recorded,
                self._duplicate,
                self._quarantined,
                len(summary.trade_dates),
                self._maximum_trade_dates,
                summary.legacy_retained,
                self._maximum_archive_bytes,
                max(0, self._maximum_archive_bytes - summary.retained_bytes),
            )

    @contextmanager
    def _connection(self, database: Path, *, write: bool) -> Iterator[sqlite3.Connection]:
        target = str(database) if write else f"file:{database.resolve()}?mode=ro"
        connection = sqlite3.connect(target, timeout=5.0, uri=not write)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _database_for(self, trade_date: date) -> Path:
        if self._use_legacy_layout:
            return self._legacy_database
        return self._partitions / f"{trade_date.isoformat()}.sqlite3"

    def _initialize_database(self, database: Path) -> None:
        if database in self._initialized_databases:
            return
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection(database, write=True) as connection:
            connection.executescript(_SCHEMA)
            self._quarantined += self._quarantine_invalid_rows(connection)
        self._initialized_databases.add(database)

    def _partition_databases(self) -> tuple[Path, ...]:
        if not self._partitions.exists():
            return ()
        return tuple(path for path in sorted(self._partitions.glob("*.sqlite3")) if _partition_date(path) is not None)

    def _source_databases(self, trade_date: date | None = None) -> tuple[Path, ...]:
        databases: list[Path] = []
        if self._legacy_database.is_file():
            databases.append(self._legacy_database)
        if trade_date is None:
            databases.extend(self._partition_databases())
        else:
            partition = self._partitions / f"{trade_date.isoformat()}.sqlite3"
            if partition.is_file():
                databases.append(partition)
        return tuple(databases)

    def _existing_row(self, decision_version: str, trade_date: date, database: Path) -> sqlite3.Row | None:
        for source in dict.fromkeys((*self._source_databases(trade_date), database)):
            if not source.is_file():
                continue
            with self._connection(source, write=False) as connection:
                row = connection.execute(
                    "SELECT payload_hash, payload FROM committed_events WHERE decision_version = ?",
                    (decision_version,),
                ).fetchone()
            if row is not None:
                return cast(sqlite3.Row, row)
        return None

    def _archive_summary(self) -> _ArchiveSummary:
        retained = 0
        retained_bytes = 0
        legacy_retained = 0
        dates: set[date] = set()
        for database in self._source_databases():
            with self._connection(database, write=False) as connection:
                row = connection.execute(
                    "SELECT COUNT(*) retained, COALESCE(SUM(LENGTH(payload)), 0) retained_bytes FROM committed_events"
                ).fetchone()
                day_rows = connection.execute("SELECT DISTINCT trade_date FROM committed_events").fetchall()
            count = int(row["retained"])
            retained += count
            retained_bytes += int(row["retained_bytes"])
            dates.update(date.fromisoformat(str(item["trade_date"])) for item in day_rows)
            if database == self._legacy_database:
                legacy_retained = count
        return _ArchiveSummary(retained, retained_bytes, frozenset(dates), legacy_retained)

    def _quarantine_database_row(self, database: Path, decision_version: str, reason: str) -> None:
        if database == self._legacy_database:
            self._quarantined += 1
            return
        with self._connection(database, write=True) as connection:
            self._quarantine_row(connection, decision_version, reason)
        self._quarantined += 1

    def _quarantine_invalid_rows(self, connection: sqlite3.Connection) -> int:
        quarantined = 0
        rows = connection.execute("SELECT * FROM committed_events").fetchall()
        for row in rows:
            try:
                observation = _verified_event(row)
                if observation.event.decision_version != str(row["decision_version"]):
                    raise ValueError("research event identity mismatch")
            except (TypeError, ValueError, json.JSONDecodeError):
                self._quarantine_row(connection, str(row["decision_version"]), "startup_verification_failed")
                quarantined += 1
        return quarantined

    def _quarantine_row(self, connection: sqlite3.Connection, decision_version: str, reason: str) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO committed_event_quarantine (
                decision_version, reason, payload_hash, payload
            )
            SELECT decision_version, ?, payload_hash, payload
            FROM committed_events WHERE decision_version = ?
            """,
            (reason, decision_version),
        )
        connection.execute("DELETE FROM committed_events WHERE decision_version = ?", (decision_version,))


@dataclass(frozen=True)
class _ArchiveSummary:
    retained: int
    retained_bytes: int
    trade_dates: frozenset[date]
    legacy_retained: int


def _partition_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def _verified_event(row: sqlite3.Row) -> V2DecisionObservation:
    payload = bytes(row["payload"])
    row_schema = str(row["schema_version"])
    if row_schema not in {LEGACY_RESEARCH_EVENT_SCHEMA_VERSION, RESEARCH_EVENT_SCHEMA_VERSION}:
        raise ValueError("research event row schema is invalid")
    raw = json.loads(payload)
    if not isinstance(raw, dict) or raw.get("schema_version") != row_schema:
        raise ValueError("research event row and payload schema mismatch")
    observation = _observation_from_bytes(payload, str(row["payload_hash"]))
    event = observation.event
    if (
        event.decision_version != str(row["decision_version"])
        or event.strategy.value != str(row["strategy"])
        or event.trade_date.isoformat() != str(row["trade_date"])
        or event.observed_at.isoformat() != str(row["observed_at"])
        or event.decision_hash != str(row["decision_hash"])
    ):
        raise ValueError("research event row identity mismatch")
    return observation


def _observation_from_bytes(payload: bytes, payload_hash: str) -> V2DecisionObservation:
    if _sha256(payload) != payload_hash:
        raise ValueError("research event payload hash mismatch")
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("research event payload must be an object")
    return _observation_from_dict(cast(dict[str, object], raw))


def _observation_bytes(
    observation: V2DecisionObservation,
    *,
    schema_version: str = RESEARCH_EVENT_SCHEMA_VERSION,
) -> bytes:
    if schema_version not in {LEGACY_RESEARCH_EVENT_SCHEMA_VERSION, RESEARCH_EVENT_SCHEMA_VERSION}:
        raise ValueError("research event schema is invalid")
    audit = observation.research_audit
    expected_audit_schema = (
        LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION
        if schema_version == LEGACY_RESEARCH_EVENT_SCHEMA_VERSION
        else RESEARCH_AUDIT_SCHEMA_VERSION
    )
    if audit is not None and audit.schema_version != expected_audit_schema:
        raise ValueError("research event and audit schemas must advance together")
    event = observation.event
    payload = {
        "schema_version": schema_version,
        "event_id": event.event_id,
        "strategy": event.strategy.value,
        "trade_date": event.trade_date.isoformat(),
        "observed_at": event.observed_at.isoformat(),
        "decision_version": event.decision_version,
        "decision_hash": event.decision_hash,
        "parent_version": event.parent_version,
        "stage": event.stage,
        "input_versions": event.input_versions,
        "config_version": event.config_version,
        "strategy_version": event.strategy_version,
        "fusion_version": event.fusion_version,
        "decision_schema_version": event.schema_version,
        "filter_aggregates": event.filter_aggregates,
        "degraded_reasons": event.degraded_reasons,
        "items": tuple(_item_dict(item) for item in event.items),
        "research_audit": _audit_dict(observation.research_audit),
    }
    return json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _item_dict(item: V2CommittedDecisionItem) -> dict[str, object]:
    return {
        "code": item.code,
        "action": item.action.value,
        "selected": item.selected,
        "rank": item.rank,
        "candidate_score": item.candidate_score,
        "local_score": item.local_score,
        "final_score": item.final_score,
        "score_components": item.score_components,
        "risk_codes": item.risk_codes,
        "reason": item.reason,
    }


def _audit_dict(audit: V2CommittedResearchAudit | None) -> dict[str, object] | None:
    if audit is None:
        return None
    payload: dict[str, object] = {
        "schema_version": audit.schema_version,
        "decision_version": audit.decision_version,
        "decision_hash": audit.decision_hash,
        "input_version": audit.input_version,
        "hard_filter_aggregates": audit.hard_filter_aggregates,
        "passed_candidates": tuple(_candidate_audit_dict(item) for item in audit.passed_candidates),
        "production_local": _decision_set_audit_dict(audit.production_local),
        "research_shadow": _decision_set_audit_dict(audit.research_shadow),
        "shadow_mode": audit.shadow_mode,
        "deepseek_request_delta": audit.deepseek_request_delta,
        "content_hash": audit.content_hash,
    }
    if audit.schema_version == RESEARCH_AUDIT_SCHEMA_VERSION:
        payload.update(
            {
                "input_observed_at": (
                    audit.input_observed_at.isoformat() if audit.input_observed_at is not None else None
                ),
                "point_in_time_population": tuple(
                    _population_audit_dict(item) for item in audit.point_in_time_population
                ),
                "point_in_time_population_hash": audit.point_in_time_population_hash,
            }
        )
    return payload


def _population_audit_dict(item: V2ResearchPopulationAudit) -> dict[str, object]:
    return {
        "code": item.code,
        "board": item.board,
        "industry": item.industry,
        "feature_observed_at": item.feature_observed_at.isoformat(),
        "quote_source_time": item.quote_source_time.isoformat(),
        "quote_source": item.quote_source,
        "data_version": item.data_version,
        "is_st": item.is_st,
        "listing_date": item.listing_date.isoformat() if item.listing_date is not None else None,
        "is_relisted_first_session": item.is_relisted_first_session,
        "is_delisting_period_first_session": item.is_delisting_period_first_session,
        "has_delisting_name": item.has_delisting_name,
        "structured_risk_values": item.structured_risk_values,
        "external_risk_facts": tuple(_risk_fact_audit_dict(fact) for fact in item.external_risk_facts),
        "filter_reasons": item.filter_reasons,
        "disposition": item.disposition,
        "requested_for_refresh": item.requested_for_refresh,
    }


def _risk_fact_audit_dict(item: V2ResearchRiskFactAudit) -> dict[str, object]:
    return {
        "risk_code": item.risk_code,
        "source": item.source,
        "observed_at": item.observed_at.isoformat(),
        "confidence": item.confidence,
        "veto": item.veto,
    }


def _candidate_audit_dict(item: V2ResearchCandidateAudit) -> dict[str, object]:
    return {
        "code": item.code,
        "board": item.board,
        "industry": item.industry,
        "candidate_components": item.candidate_components,
        "missing_mask": item.missing_mask,
        "coverage_ratio": item.coverage_ratio,
        "board_reliability": item.board_reliability,
        "candidate_score": item.candidate_score,
        "candidate_rank": item.candidate_rank,
        "production_top120": item.production_top120,
        "preselection_status": item.preselection_status,
        "optimistic_upper_bound": item.optimistic_upper_bound,
        "upper_bound_status": item.upper_bound_status,
        "upper_bound_protected": item.upper_bound_protected,
    }


def _decision_set_audit_dict(item: V2ResearchDecisionSetAudit) -> dict[str, object]:
    return {
        "decision_version": item.decision_version,
        "candidates": tuple(_decision_candidate_audit_dict(candidate) for candidate in item.candidates),
    }


def _decision_candidate_audit_dict(item: V2ResearchDecisionCandidateAudit) -> dict[str, object]:
    return {
        "code": item.code,
        "components": item.components,
        "component_coverage_ratio": item.component_coverage_ratio,
        "base_score": item.base_score,
        "local_risk_codes": item.local_risk_codes,
        "local_risk_penalty": item.local_risk_penalty,
        "local_score": item.local_score,
        "reused_deepseek_facts": item.reused_deepseek_facts,
        "fusion_applied": item.fusion_applied,
        "deepseek_risk_codes": item.deepseek_risk_codes,
        "deepseek_risk_penalty": item.deepseek_risk_penalty,
        "final_score": item.final_score,
        "action": item.action,
        "selected": item.selected,
        "rank": item.rank,
        "board_rank": item.board_rank,
        "skip_reason": item.skip_reason,
    }


def _observation_from_dict(raw: dict[str, object]) -> V2DecisionObservation:
    schema_version = raw.get("schema_version")
    if schema_version not in {
        LEGACY_RESEARCH_EVENT_SCHEMA_VERSION,
        RESEARCH_EVENT_SCHEMA_VERSION,
    }:
        raise ValueError("research event schema is invalid")
    stage = _text(raw, "stage")
    if stage not in {"local", "hybrid"}:
        raise ValueError("research event stage is invalid")
    event = V2DecisionCommitted(
        event_id=_text(raw, "event_id"),
        strategy=Strategy(_text(raw, "strategy")),
        trade_date=date.fromisoformat(_text(raw, "trade_date")),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        decision_version=_text(raw, "decision_version"),
        decision_hash=_text(raw, "decision_hash"),
        parent_version=_optional_text(raw.get("parent_version")),
        stage=cast(DecisionStage, stage),
        input_versions=_text_pairs(raw.get("input_versions"), "input_versions"),
        config_version=_text(raw, "config_version"),
        strategy_version=_text(raw, "strategy_version"),
        fusion_version=_text(raw, "fusion_version"),
        schema_version=_text(raw, "decision_schema_version"),
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
    return V2DecisionObservation(event, audit)


def _item(raw: dict[str, object]) -> V2CommittedDecisionItem:
    return V2CommittedDecisionItem(
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


def _audit(raw: dict[str, object]) -> V2CommittedResearchAudit:
    schema_version = _text(raw, "schema_version")
    is_v2 = schema_version == RESEARCH_AUDIT_SCHEMA_VERSION
    if schema_version not in {LEGACY_RESEARCH_AUDIT_SCHEMA_VERSION, RESEARCH_AUDIT_SCHEMA_VERSION}:
        raise ValueError("research audit schema is invalid")
    audit = V2CommittedResearchAudit(
        decision_version=_text(raw, "decision_version"),
        decision_hash=_text(raw, "decision_hash"),
        input_version=_text(raw, "input_version"),
        hard_filter_aggregates=_count_pairs(raw.get("hard_filter_aggregates")),
        passed_candidates=tuple(
            _candidate_audit(_object(item, "passed_candidate"))
            for item in _list(raw.get("passed_candidates"), "passed_candidates")
        ),
        production_local=_decision_set_audit(_object(raw.get("production_local"), "production_local")),
        research_shadow=_decision_set_audit(_object(raw.get("research_shadow"), "research_shadow")),
        shadow_mode=cast(ShadowMode, _text(raw, "shadow_mode")),
        deepseek_request_delta=_integer(raw, "deepseek_request_delta"),
        schema_version=schema_version,
        input_observed_at=(datetime.fromisoformat(_text(raw, "input_observed_at")) if is_v2 else None),
        point_in_time_population=(
            tuple(
                _population_audit(_object(item, "point_in_time_population"))
                for item in _list(raw.get("point_in_time_population"), "point_in_time_population")
            )
            if is_v2
            else ()
        ),
        point_in_time_population_hash=(_text(raw, "point_in_time_population_hash") if is_v2 else ""),
    )
    if audit.content_hash != _text(raw, "content_hash"):
        raise ValueError("research audit content hash mismatch")
    return audit


def _population_audit(raw: dict[str, object]) -> V2ResearchPopulationAudit:
    listing_date_raw = raw.get("listing_date")
    return V2ResearchPopulationAudit(
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


def _risk_fact_audit(raw: dict[str, object]) -> V2ResearchRiskFactAudit:
    return V2ResearchRiskFactAudit(
        risk_code=_text(raw, "risk_code"),
        source=_text(raw, "source"),
        observed_at=datetime.fromisoformat(_text(raw, "observed_at")),
        confidence=_number(raw, "confidence"),
        veto=_boolean(raw, "veto"),
    )


def _candidate_audit(raw: dict[str, object]) -> V2ResearchCandidateAudit:
    return V2ResearchCandidateAudit(
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
        preselection_status=_text(raw, "preselection_status"),
        optimistic_upper_bound=_optional_number(raw.get("optimistic_upper_bound")),
        upper_bound_status=cast(Literal["not_computed"], _text(raw, "upper_bound_status")),
        upper_bound_protected=_boolean(raw, "upper_bound_protected"),
    )


def _decision_set_audit(raw: dict[str, object]) -> V2ResearchDecisionSetAudit:
    return V2ResearchDecisionSetAudit(
        decision_version=_text(raw, "decision_version"),
        candidates=tuple(
            _decision_candidate_audit(_object(item, "decision_candidate"))
            for item in _list(raw.get("candidates"), "decision_candidates")
        ),
    )


def _decision_candidate_audit(raw: dict[str, object]) -> V2ResearchDecisionCandidateAudit:
    return V2ResearchDecisionCandidateAudit(
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


def _object(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], raw)


def _list(raw: object, label: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list")
    return raw


def _text(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be text")
    return value


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("optional text is invalid")
    return raw


def _boolean(raw: dict[str, object], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _optional_boolean(raw: object) -> bool | None:
    if raw is None:
        return None
    if not isinstance(raw, bool):
        raise ValueError("optional boolean is invalid")
    return raw


def _integer(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _number(raw: dict[str, object], key: str) -> float:
    value = raw.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _optional_number(raw: object) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ValueError("optional number is invalid")
    return float(raw)


def _strings(raw: object, label: str) -> list[str]:
    values = _list(raw, label)
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{label} must contain text")
    return cast(list[str], values)


def _text_pairs(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for item in _list(raw, label):
        values = _list(item, label)
        if len(values) != 2 or any(not isinstance(value, str) for value in values):
            raise ValueError(f"{label} entries are invalid")
        result.append((cast(str, values[0]), cast(str, values[1])))
    return tuple(result)


def _count_pairs(raw: object) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for item in _list(raw, "filter_aggregates"):
        values = _list(item, "filter_aggregate")
        if len(values) != 2 or not isinstance(values[0], str) or not isinstance(values[1], int):
            raise ValueError("filter aggregate is invalid")
        result.append((values[0], values[1]))
    return tuple(result)


def _score_pairs(raw: object) -> tuple[tuple[str, float | None], ...]:
    result: list[tuple[str, float | None]] = []
    for item in _list(raw, "score_components"):
        values = _list(item, "score_component")
        if len(values) != 2 or not isinstance(values[0], str):
            raise ValueError("score component is invalid")
        result.append((values[0], _optional_number(values[1])))
    return tuple(result)


def _required_score_pairs(raw: object) -> tuple[tuple[str, float], ...]:
    result: list[tuple[str, float]] = []
    for name, value in _score_pairs(raw):
        if value is None:
            raise ValueError("required score component cannot be null")
        result.append((name, value))
    return tuple(result)


def _optional_number_pairs(raw: object, label: str) -> tuple[tuple[str, float | None], ...]:
    result: list[tuple[str, float | None]] = []
    for item in _list(raw, label):
        values = _list(item, label)
        if len(values) != 2 or not isinstance(values[0], str):
            raise ValueError(f"{label} entries are invalid")
        result.append((values[0], _optional_number(values[1])))
    return tuple(result)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS committed_events (
    decision_version TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS committed_events_trade_date
ON committed_events(trade_date DESC, strategy, observed_at DESC);
CREATE TABLE IF NOT EXISTS committed_event_quarantine (
    decision_version TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
"""


__all__ = [
    "LEGACY_RESEARCH_EVENT_SCHEMA_VERSION",
    "RESEARCH_EVENT_SCHEMA_VERSION",
    "ResearchTraceCapacityError",
    "ResearchTraceConflictError",
    "ResearchTraceLimits",
    "SQLiteV2ResearchTraceStore",
    "V2ResearchTradeDateObservation",
    "V2ResearchTraceStatus",
]
