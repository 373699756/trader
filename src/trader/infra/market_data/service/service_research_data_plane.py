"""Persistence helpers for research component risk evidence."""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from threading import Lock
from typing import Protocol, cast

from trader.application.ports.data_plane import (
    DataPlaneConflictError,
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
    SourceCursorRecord,
)
from trader.domain.market.research import (
    ResearchAnnouncement,
    ResearchObservation,
    corporate_risk_facts_from_announcements,
)
from trader.infra.market_data.providers.cninfo import (
    CNINFO_ANNOUNCEMENT_PREFIX,
    CNINFO_COMPONENT_PREFIX,
    CNINFO_CURSOR_PREFIX,
    CNINFO_SOURCE,
)
from trader.infra.market_data.service.market_cache_identity import (
    _merge_research_observation,
    _research_data_version,
    _research_source_time,
)
from trader.infra.market_data.service.service_models import _ResearchEntry
from trader.infra.market_data.service.service_research_models import (
    RESEARCH_COMPONENT_IDS,
    ResearchComponentStatus,
    ResearchLoaderStatus,
    research_component_statuses,
)

_LOGGER = logging.getLogger(__name__)
_RISK_COMPONENT_EVIDENCE_PREFIX = "risk-component:"
_RESEARCH_SOURCE = "akshare"
_COMPONENT_PREFIXES = (_RISK_COMPONENT_EVIDENCE_PREFIX, CNINFO_COMPONENT_PREFIX)
_STATUS_PRIORITY: Mapping[ResearchComponentStatus, int] = {
    "unknown": 0,
    "stale": 1,
    "known_clear": 2,
    "known_risk": 3,
}


class _ResearchComponentPersistenceState(Protocol):
    _lock: Lock
    _component_statuses: dict[str, dict[str, ResearchComponentStatus]]


class _ResearchDataPlane(Protocol):
    def save_risk_evidence_recent(self, record: RiskEvidenceRecord) -> None: ...

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


def component_statuses_for_code(
    state: _ResearchComponentPersistenceState,
    code: str,
    observation: ResearchObservation,
) -> tuple[ResearchComponentStatus, ...]:
    computed = research_component_statuses(observation)
    restored = state._component_statuses.get(code, {})
    if not restored:
        return computed
    return tuple(
        restored.get(component, status) for component, status in zip(RESEARCH_COMPONENT_IDS, computed, strict=True)
    )


def recover_research_component_statuses(
    state: _ResearchComponentPersistenceState,
    data_plane: _ResearchDataPlane,
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
    cninfo_announcements: dict[str, list[ResearchAnnouncement]] = {}
    for record in records:
        component = _component_from_evidence_id(record.evidence_id)
        if component is not None and record.source in {_RESEARCH_SOURCE, CNINFO_SOURCE}:
            status = _parse_component_risk_status(record.payload)
            if status is None:
                _LOGGER.warning(
                    "research recovery skipped invalid status payload for component=%s code=%s",
                    component,
                    record.code,
                )
                continue
            current = restored.setdefault(record.code, {}).get(component)
            restored[record.code][component] = status if current is None else _merge_component_status(current, status)
            continue
        if record.source == CNINFO_SOURCE and record.evidence_id.startswith(CNINFO_ANNOUNCEMENT_PREFIX):
            announcement = _announcement_from_risk_evidence(record)
            if announcement is not None:
                cninfo_announcements.setdefault(record.code, []).append(announcement)
    if not restored:
        restored = {}
    with state._lock:
        state._component_statuses.update(restored)
    _recover_cninfo_observations(state, data_plane, cninfo_announcements)


def persist_research_component_statuses(
    state: _ResearchComponentPersistenceState,
    data_plane: _ResearchDataPlane,
    code: str,
    observed_at: datetime,
    observation: ResearchObservation,
) -> None:
    component_statuses = research_component_statuses(observation)
    source_time = _research_source_time(observation) or observed_at
    if source_time > observed_at:
        source_time = observed_at
    source_version = _research_data_version(observation)

    with state._lock:
        current = state._component_statuses.setdefault(code, {})
        for component, status in zip(RESEARCH_COMPONENT_IDS, component_statuses, strict=True):
            current.setdefault(component, status)

    for component, status in zip(RESEARCH_COMPONENT_IDS, component_statuses, strict=True):
        payload = {"status": status}
        try:
            data_plane.save_risk_evidence_recent(
                RiskEvidenceRecord(
                    code=code,
                    observed_at=observed_at,
                    source=_RESEARCH_SOURCE,
                    source_time=source_time,
                    data_version=source_version,
                    evidence_id=f"{_RISK_COMPONENT_EVIDENCE_PREFIX}{component}",
                    payload=payload,
                )
            )
        except DataPlaneConflictError:
            _LOGGER.debug(
                "research risk component retained an existing same-time record for component=%s code=%s",
                component,
                code,
            )
        except DataPlaneUnavailableError:
            _LOGGER.warning(
                "research data plane unavailable while saving risk component status for component=%s code=%s",
                component,
                code,
            )
        except Exception as exc:
            _LOGGER.warning(
                "research risk component persistence failed for component=%s code=%s error=%s",
                component,
                code,
                type(exc).__name__,
            )


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
    if not hasattr(state, "_entries") or not hasattr(state, "_monotonic") or not hasattr(state, "_ttl_seconds"):
        return
    entries = state._entries  # type: ignore[attr-defined]
    monotonic = state._monotonic  # type: ignore[attr-defined]
    ttl_seconds = state._ttl_seconds  # type: ignore[attr-defined]
    restored: dict[tuple[str, bool], _ResearchEntry] = {}
    for code, announcements in announcements_by_code.items():
        ordered = tuple(sorted(announcements, key=lambda item: (item.published_at, item.announcement_id)))
        facts = corporate_risk_facts_from_announcements(ordered)
        observation = ResearchObservation(
            announcements=ordered,
            announcements_available=True,
            corporate_risk_facts=facts,
            corporate_risk_history_complete=history_complete_by_code.get(code, False),
            corporate_risk_registry_version=_cninfo_registry_version(code, ordered),
        )
        key = (code, True)
        previous = entries.get(key)
        if previous is not None:
            observation = _merge_research_observation(previous, observation)
        restored[key] = _ResearchEntry(observation, monotonic() + ttl_seconds)
    if not restored:
        return
    with state._lock:
        entries.update(restored)


def _cninfo_history_complete_by_code(data_plane: _ResearchDataPlane) -> dict[str, bool]:
    loader = getattr(data_plane, "load_source_cursor_recent_records", None)
    if loader is None:
        return {}
    try:
        records = loader()
    except DataPlaneUnavailableError:
        return {}
    except Exception:
        return {}
    result: dict[str, bool] = {}
    for record in records:
        if record.source != CNINFO_SOURCE or not record.cursor_name.startswith(CNINFO_CURSOR_PREFIX):
            continue
        code = record.cursor_name.removeprefix(CNINFO_CURSOR_PREFIX)
        history_complete = record.payload.get("history_complete") if isinstance(record.payload, Mapping) else None
        result[code] = bool(history_complete)
    return result


def _cninfo_registry_version(code: str, announcements: tuple[ResearchAnnouncement, ...]) -> str:
    material = "|".join(f"{item.announcement_id}:{item.published_at.isoformat()}" for item in announcements)
    digest = hashlib.sha256(f"{code}|{material}".encode()).hexdigest()[:16]
    return f"cninfo-risk-registry:{digest}"


def loader_status(state: _ResearchLoaderStatusState) -> ResearchLoaderStatus:
    with state._lock:
        observations = tuple(entry.observation for (code, structured), entry in state._entries.items() if structured)
        coverage = tuple(
            _coverage_from_statuses(component_statuses_for_code(state, code, entry.observation))
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
            corporate_risk_registry_versions=tuple(
                sorted(
                    {
                        observation.corporate_risk_registry_version
                        for observation in observations
                        if observation.corporate_risk_registry_version
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
