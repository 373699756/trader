"""Persistence helpers for research component risk evidence."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from threading import Lock
from typing import Protocol, cast

from trader.recommendation.application.pipeline.data_source.input_identity import InputVersionClock
from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneConflictError,
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.domain.market.research import (
    CorporateRiskFactsFromAnnouncements,
    ResearchAnnouncement,
    ResearchObservation,
)
from trader.recommendation.infra.market_data.announcement_sync import (
    CNINFO_ANNOUNCEMENT_PREFIX,
    CNINFO_COMPONENT_PREFIX,
    CNINFO_CURSOR_PREFIX,
    CNINFO_SOURCE,
)
from trader.recommendation.infra.market_data.market_cache_identity import (
    _deserialize_research_observation,
    _research_data_version,
    _research_source_time,
    _serialize_research_observation,
)
from trader.recommendation.infra.market_data.market_feature_cache_entries import _ResearchEntry
from trader.recommendation.infra.market_data.research_load_status import (
    RESEARCH_COMPONENT_IDS,
    ResearchComponentStatus,
    ResearchComponentStatuses,
    ResearchLoaderStatus,
)

_LOGGER = logging.getLogger(__name__)
_RISK_COMPONENT_EVIDENCE_PREFIX = "risk-component:"
_RESEARCH_OBSERVATION_EVIDENCE_ID = "risk-observation:structured"
_RESEARCH_SOURCE = "akshare"
_COMPONENT_PREFIXES = (_RISK_COMPONENT_EVIDENCE_PREFIX, CNINFO_COMPONENT_PREFIX)
_STATUS_PRIORITY: Mapping[ResearchComponentStatus, int] = {
    "unknown": 0,
    "stale": 1,
    "known_clear": 2,
    "known_risk": 3,
}


class _ResearchComponentPersistenceState(Protocol):
    _versions: InputVersionClock
    _lock: Lock
    _component_statuses: dict[str, dict[str, ResearchComponentStatus]]
    _entries: dict[tuple[str, bool], _ResearchEntry]
    _monotonic: Callable[[], float]
    _ttl_seconds: float


class _ResearchDataPlane(Protocol):
    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None: ...

    def save_risk_evidence_recent_records(self, records: Sequence[RiskEvidenceRecord]) -> None: ...

    def load_risk_evidence_recent_records(
        self, codes: Sequence[str] | None = None
    ) -> tuple[RiskEvidenceRecord, ...]: ...

    def load_source_cursor_recent_records(
        self, cursor_names: Sequence[str] | None = None
    ) -> tuple[SourceCursorRecord, ...]: ...


class _ResearchLoaderStatusState(_ResearchComponentPersistenceState, Protocol):
    _entries: dict[tuple[str, bool], _ResearchEntry]
    _success_count: int
    _error_count: int
    _planned_count: int
    _timeout_count: int
    _consecutive_failures: int
    _open_until: float
    _latencies_ms: deque[float]
    _latest_source_time: datetime | None
    _last_error: str
    _out_of_order_count: int
    _lock: Lock
    _monotonic: Callable[[], float]


def ComponentStatusesForCode(
    state: _ResearchComponentPersistenceState,
    code: str,
    observation: ResearchObservation,
) -> tuple[ResearchComponentStatus, ...]:
    computed = ResearchComponentStatuses(observation)
    restored = state._component_statuses.get(code, {})
    if not restored:
        return computed
    return tuple(
        restored.get(component, status) for component, status in zip(RESEARCH_COMPONENT_IDS, computed, strict=True)
    )


def RecoverResearchComponentStatuses(
    state: _ResearchComponentPersistenceState,
    data_plane: _ResearchDataPlane,
    observed_at: datetime,
    *,
    retain_stale: bool = False,
) -> None:
    try:
        records = data_plane.load_risk_evidence_recent_records()
    except DataPlaneUnavailableError:
        _LOGGER.warning("research data plane unavailable during recovery")
        return
    except Exception as exc:
        _LOGGER.warning("research recovery read failed: %s", type(exc).__name__)
        return

    restored: dict[str, dict[str, ResearchComponentStatus]] = {}
    restored_observations: dict[str, tuple[ResearchObservation, datetime]] = {}
    cninfo_announcements: dict[str, list[ResearchAnnouncement]] = {}
    for record in records:
        observation = _recovered_observation(record, observed_at)
        if observation is not None:
            restored_observations[record.code] = (observation, record.observed_at)
            continue
        component_status = _recovered_component_status(record)
        if component_status is not None:
            component, status = component_status
            current = restored.setdefault(record.code, {}).get(component)
            restored[record.code][component] = status if current is None else _merge_component_status(current, status)
            continue
        announcement = _recovered_cninfo_announcement(record)
        if announcement is not None:
            cninfo_announcements.setdefault(record.code, []).append(announcement)
    with state._lock:
        state._component_statuses.update(restored)
        for code, (observation, cached_at) in restored_observations.items():
            remaining = _recovered_remaining_seconds(
                observation,
                observed_at=observed_at,
                cached_at=cached_at,
                ttl_seconds=state._ttl_seconds,
                retain_stale=retain_stale,
            )
            state._entries[(code, True)] = _ResearchEntry(observation, state._monotonic() + remaining)
    _recover_cninfo_observations(state, data_plane, cninfo_announcements)


def _recovered_observation(record: RiskEvidenceRecord, observed_at: datetime) -> ResearchObservation | None:
    if record.evidence_id != _RESEARCH_OBSERVATION_EVIDENCE_ID:
        return None
    if record.observed_at > observed_at:
        _LOGGER.warning("research recovery skipped future observation code=%s", record.code)
        return None
    try:
        return _deserialize_research_observation(record.payload)
    except (TypeError, ValueError, KeyError):
        _LOGGER.warning("research recovery skipped invalid observation code=%s", record.code)
        return None


def _recovered_component_status(
    record: RiskEvidenceRecord,
) -> tuple[str, ResearchComponentStatus] | None:
    component = _component_from_evidence_id(record.evidence_id)
    if component is None or record.source not in {_RESEARCH_SOURCE, CNINFO_SOURCE}:
        return None
    status = _parse_component_risk_status(record.payload)
    if status is None:
        _LOGGER.warning(
            "research recovery skipped invalid status payload for component=%s code=%s",
            component,
            record.code,
        )
        return None
    return component, status


def _recovered_cninfo_announcement(record: RiskEvidenceRecord) -> ResearchAnnouncement | None:
    if record.source != CNINFO_SOURCE or not record.evidence_id.startswith(CNINFO_ANNOUNCEMENT_PREFIX):
        return None
    return _announcement_from_risk_evidence(record)


def _recovered_remaining_seconds(
    observation: ResearchObservation,
    *,
    observed_at: datetime,
    cached_at: datetime,
    ttl_seconds: float,
    retain_stale: bool,
) -> float:
    effective_ttl = min(60.0, ttl_seconds) if observation.source_errors else ttl_seconds
    if retain_stale:
        return effective_ttl
    age_seconds = max(0.0, (observed_at - cached_at).total_seconds())
    return max(0.0, effective_ttl - age_seconds)


def PersistResearchComponentStatuses(
    state: _ResearchComponentPersistenceState,
    data_plane: _ResearchDataPlane,
    code: str,
    observed_at: datetime,
    observation: ResearchObservation,
) -> None:
    PersistResearchComponentStatusBatch(state, data_plane, observed_at, {code: observation})


def PersistResearchComponentStatusBatch(
    state: _ResearchComponentPersistenceState,
    data_plane: _ResearchDataPlane,
    observed_at: datetime,
    observations: Mapping[str, ResearchObservation],
) -> None:
    records: list[RiskEvidenceRecord] = []
    statuses_by_code: dict[str, tuple[ResearchComponentStatus, ...]] = {}
    for code, observation in observations.items():
        component_statuses = ResearchComponentStatuses(observation)
        statuses_by_code[code] = component_statuses
        source_time = min(_research_source_time(observation) or observed_at, observed_at)
        source_version = _research_data_version(observation)
        records.append(
            RiskEvidenceRecord(
                code=code,
                observed_at=observed_at,
                source=_RESEARCH_SOURCE,
                source_time=source_time,
                data_version=source_version,
                evidence_id=_RESEARCH_OBSERVATION_EVIDENCE_ID,
                payload=cast(JsonObject, _serialize_research_observation(observation)),
            )
        )
        records.extend(
            RiskEvidenceRecord(
                code=code,
                observed_at=observed_at,
                source=_RESEARCH_SOURCE,
                source_time=source_time,
                data_version=source_version,
                evidence_id=f"{_RISK_COMPONENT_EVIDENCE_PREFIX}{component}",
                payload={"status": status},
            )
            for component, status in zip(RESEARCH_COMPONENT_IDS, component_statuses, strict=True)
        )

    with state._lock:
        for code, component_statuses in statuses_by_code.items():
            current = state._component_statuses.setdefault(code, {})
            for component, status in zip(RESEARCH_COMPONENT_IDS, component_statuses, strict=True):
                current.setdefault(component, status)

    try:
        data_plane.save_risk_evidence_recent_records(records)
    except DataPlaneConflictError:
        _LOGGER.debug("research risk batch retained existing same-time records rows=%s", len(records))
    except DataPlaneUnavailableError:
        _LOGGER.warning("research data plane unavailable while saving risk batch rows=%s", len(records))
    except Exception as exc:
        _LOGGER.warning("research risk batch persistence failed rows=%s error=%s", len(records), type(exc).__name__)


def _parse_component_risk_status(payload: object) -> ResearchComponentStatus | None:
    if not isinstance(payload, Mapping):
        return None
    raw_status = payload.get("status")
    if not isinstance(raw_status, str):
        return None
    if raw_status not in {"known_clear", "known_risk", "unknown", "stale"}:
        return None
    return cast(ResearchComponentStatus, raw_status)


def _component_from_evidence_id(evidence_id: str) -> str | None:
    for prefix in _COMPONENT_PREFIXES:
        if not evidence_id.startswith(prefix):
            continue
        component = evidence_id.removeprefix(prefix)
        return component if component in RESEARCH_COMPONENT_IDS else None
    return None


def _merge_component_status(
    current: ResearchComponentStatus,
    incoming: ResearchComponentStatus,
) -> ResearchComponentStatus:
    return incoming if _STATUS_PRIORITY[incoming] > _STATUS_PRIORITY[current] else current


def _announcement_from_risk_evidence(record: RiskEvidenceRecord) -> ResearchAnnouncement | None:
    payload = record.payload
    if not isinstance(payload, Mapping):
        return None
    title = payload.get("title")
    published_at = payload.get("published_at")
    if not isinstance(title, str) or not title.strip() or not isinstance(published_at, str):
        return None
    try:
        parsed_at = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        return None
    return ResearchAnnouncement(
        title=title,
        published_at=parsed_at,
        announcement_id=record.evidence_id,
        source="issuer_disclosure",
    )


def _recover_cninfo_observations(
    state: _ResearchComponentPersistenceState,
    data_plane: _ResearchDataPlane,
    announcements_by_code: dict[str, list[ResearchAnnouncement]],
) -> None:
    if not announcements_by_code:
        return
    history_complete_by_code = _cninfo_history_complete_by_code(data_plane)
    entries = state._entries
    monotonic = state._monotonic
    ttl_seconds = state._ttl_seconds
    restored: dict[tuple[str, bool], _ResearchEntry] = {}
    for code, announcements in announcements_by_code.items():
        ordered = tuple(sorted(announcements, key=lambda item: (item.published_at, item.announcement_id)))
        facts = CorporateRiskFactsFromAnnouncements(ordered)
        key = (code, True)
        previous = entries.get(key)
        if previous is None:
            observation = ResearchObservation(
                announcements=ordered,
                announcements_available=True,
                corporate_risk_facts=facts,
                corporate_risk_history_complete=history_complete_by_code.get(code, False),
                corporate_risk_evidence_version=_cninfo_evidence_version(code, ordered),
            )
        else:
            previous_observation = previous.observation
            merged_announcements = tuple(
                sorted(
                    {item.announcement_id: item for item in (*previous_observation.announcements, *ordered)}.values(),
                    key=lambda item: (item.published_at, item.announcement_id),
                )
            )
            merged_facts = tuple(
                sorted(
                    {
                        (item.category, item.evidence_id): item
                        for item in (*previous_observation.corporate_risk_facts, *facts)
                    }.values(),
                    key=lambda item: (item.announced_at, item.category.value, item.evidence_id),
                )
            )
            observation = replace(
                previous_observation,
                announcements=merged_announcements,
                announcements_available=True,
                corporate_risk_facts=merged_facts,
                corporate_risk_history_complete=(
                    previous_observation.corporate_risk_history_complete or history_complete_by_code.get(code, False)
                ),
                corporate_risk_evidence_version=_cninfo_evidence_version(code, merged_announcements),
            )
        restored[key] = _ResearchEntry(
            replace(observation, data_version=state._versions.advance("research")), monotonic() + ttl_seconds
        )
    if not restored:
        return
    with state._lock:
        entries.update(restored)
        for (code, _structured), entry in restored.items():
            current = state._component_statuses.setdefault(code, {})
            for component, status in zip(
                RESEARCH_COMPONENT_IDS,
                ResearchComponentStatuses(entry.observation),
                strict=True,
            ):
                if status == "known_risk":
                    current[component] = status


def _cninfo_history_complete_by_code(data_plane: _ResearchDataPlane) -> dict[str, bool]:
    try:
        records = data_plane.load_source_cursor_recent_records()
    except DataPlaneUnavailableError:
        return {}
    result: dict[str, bool] = {}
    for record in records:
        if record.source != CNINFO_SOURCE or not record.cursor_name.startswith(CNINFO_CURSOR_PREFIX):
            continue
        code = record.cursor_name.removeprefix(CNINFO_CURSOR_PREFIX)
        history_complete = record.payload.get("history_complete") if isinstance(record.payload, Mapping) else None
        result[code] = bool(history_complete)
    return result


def _cninfo_evidence_version(code: str, announcements: tuple[ResearchAnnouncement, ...]) -> str:
    coordinates = tuple((item.announcement_id, item.published_at.isoformat()) for item in announcements)
    return f"cninfo-risk-evidence:{code}:{coordinates!r}"


def LoaderStatus(state: _ResearchLoaderStatusState) -> ResearchLoaderStatus:
    with state._lock:
        observations = tuple(entry.observation for (code, structured), entry in state._entries.items() if structured)
        coverage = tuple(
            _coverage_from_statuses(ComponentStatusesForCode(state, code, entry.observation))
            for (code, structured), entry in state._entries.items()
            if structured
        )
        return ResearchLoaderStatus(
            entries=len(state._entries),
            success_count=state._success_count,
            error_count=state._error_count,
            planned_count=state._planned_count,
            timeout_count=state._timeout_count,
            consecutive_failures=state._consecutive_failures,
            circuit_open=state._open_until > state._monotonic(),
            latencies_ms=tuple(state._latencies_ms),
            latest_source_time=state._latest_source_time,
            last_error=state._last_error,
            out_of_order_count=state._out_of_order_count,
            corporate_risk_covered_count=sum(
                observation.corporate_risk_history_complete for observation in observations
            ),
            corporate_risk_fact_count=sum(len(observation.corporate_risk_facts) for observation in observations),
            corporate_risk_evidence_versions=tuple(
                sorted(
                    {
                        observation.corporate_risk_evidence_version
                        for observation in observations
                        if observation.corporate_risk_evidence_version
                    }
                )
            ),
            verified_count=sum(all(item) for item in coverage),
            partial_count=sum(any(item) and not all(item) for item in coverage),
            unavailable_count=sum(not any(item) for item in coverage),
            financial_covered_count=sum(item[0] for item in coverage),
            announcements_covered_count=sum(item[1] for item in coverage),
            pledge_covered_count=sum(item[2] for item in coverage),
            unlock_covered_count=sum(item[3] for item in coverage),
        )


def _coverage_from_statuses(statuses: tuple[ResearchComponentStatus, ...]) -> tuple[bool, bool, bool, bool]:
    return (
        statuses[0] in {"known_clear", "known_risk"},
        statuses[1] in {"known_clear", "known_risk"},
        statuses[2] in {"known_clear", "known_risk"},
        statuses[3] in {"known_clear", "known_risk"},
    )
