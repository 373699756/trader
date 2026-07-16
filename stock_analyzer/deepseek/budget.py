from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol

from .. import config
from ..strategies.types import storage_strategy_name

DAILY_HARD_LIMIT = 188
DEFAULT_STRATEGY_LIMITS = {
    "today_term": 70,
    "tomorrow_picks": 45,
    "swing_picks": 35,
    "long_term_watch": 18,
    "shared_preheat": 15,
    "emergency_reserved": 5,
}
DEFAULT_WINDOW_LIMITS = {
    "shared_preheat": 15,
    "today_open_observe": 15,
    "today_main": 42,
    "today_late": 13,
    "afternoon_main": 65,
    "final_supplement": 38,
    "emergency_reserved": 5,
}
_ALL_RESEARCH_STRATEGIES = {
    "today_term",
    "tomorrow_picks",
    "swing_picks",
    "long_term_watch",
}


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    status: str
    phase: str
    budget_bucket: str
    reason: str = ""


class _BudgetRepository(Protocol):
    def connect(self) -> sqlite3.Connection: ...


class _BudgetStore(Protocol):
    repository: _BudgetRepository


def _configured_mapping(name: str, default: dict[str, int]) -> dict[str, int]:
    raw = getattr(config, name, default)
    if not isinstance(raw, dict):
        return dict(default)
    result = dict(default)
    for key, value in raw.items():
        try:
            result[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return result


def daily_hard_limit() -> int:
    try:
        configured = int(
            getattr(
                config,
                "DEEPSEEK_DAILY_API_HARD_LIMIT",
                getattr(config, "DEEPSEEK_DAILY_CALL_LIMIT", DAILY_HARD_LIMIT),
            )
        )
    except (TypeError, ValueError):
        configured = 0
    return max(0, min(DAILY_HARD_LIMIT, configured))


def strategy_limits() -> dict[str, int]:
    return _configured_mapping("DEEPSEEK_STRATEGY_CALL_LIMITS", DEFAULT_STRATEGY_LIMITS)


def window_limits() -> dict[str, int]:
    return _configured_mapping("DEEPSEEK_WINDOW_CALL_LIMITS", DEFAULT_WINDOW_LIMITS)


def phase_at(timestamp: datetime, *, emergency: bool = False) -> str:
    if emergency:
        return "emergency_reserved"
    current = timestamp.time().replace(second=0, microsecond=0)
    if time(9, 15) <= current <= time(9, 25):
        return "shared_preheat"
    if time(9, 30) <= current < time(9, 36):
        return "today_open_observe"
    if time(9, 36) <= current < time(10, 30):
        return "today_main"
    if time(10, 30) <= current <= time(11, 20):
        return "today_late"
    if time(13, 0) <= current <= time(14, 0):
        return "afternoon_main"
    if time(14, 20) <= current < time(14, 48):
        return "final_supplement"
    return "closed"


def strategies_for_phase(phase: str) -> tuple[str, ...]:
    if phase == "shared_preheat" or phase == "final_supplement":
        return ("today_term", "tomorrow_picks", "swing_picks", "long_term_watch")
    if phase in {"today_open_observe", "today_main", "today_late"}:
        return ("today_term",)
    if phase == "afternoon_main":
        return ("tomorrow_picks", "swing_picks", "long_term_watch")
    if phase == "emergency_reserved":
        return tuple(sorted(_ALL_RESEARCH_STRATEGIES))
    return ()


def _budget_bucket(strategy: str, phase: str) -> str:
    if phase in {"shared_preheat", "emergency_reserved"}:
        return phase
    return strategy


def reserve_api_call(
    store: _BudgetStore,
    batch_id: str,
    strategy_name: str,
    requested_at: datetime,
    *,
    emergency: bool = False,
) -> BudgetReservation:
    strategy = storage_strategy_name(strategy_name)
    phase = phase_at(requested_at, emergency=emergency)
    bucket = _budget_bucket(strategy, phase)
    if phase == "closed":
        return BudgetReservation(False, "deadline_skipped", phase, bucket, "outside_deepseek_execution_windows")
    if strategy not in strategies_for_phase(phase):
        return BudgetReservation(False, "deadline_skipped", phase, bucket, "strategy_not_allowed_in_phase")

    total_limit = daily_hard_limit()
    per_strategy = strategy_limits()
    per_window = window_limits()
    day = requested_at.date().isoformat()
    try:
        with store.repository.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT strategy_name, call_phase, budget_bucket, requested_at
                FROM deepseek_analysis_batches
                WHERE api_called = 1 AND substr(requested_at, 1, 10) = ?
                """,
                (day,),
            ).fetchall()
            if len(rows) >= total_limit:
                return BudgetReservation(False, "daily_call_limit", phase, bucket, "daily_hard_limit")

            bucket_count = 0
            phase_count = 0
            for row in rows:
                row_strategy = storage_strategy_name(row[0])
                row_phase = str(row[1] or "") or _phase_from_requested_at(row[3])
                row_bucket = str(row[2] or "") or _budget_bucket(row_strategy, row_phase)
                bucket_count += int(row_bucket == bucket)
                phase_count += int(row_phase == phase)
            if bucket_count >= int(per_strategy.get(bucket, 0)):
                return BudgetReservation(False, "daily_call_limit", phase, bucket, "strategy_call_limit")
            if phase_count >= int(per_window.get(phase, 0)):
                return BudgetReservation(False, "daily_call_limit", phase, bucket, "window_call_limit")

            cursor = conn.execute(
                """
                UPDATE deepseek_analysis_batches
                SET api_called = 1, call_phase = ?, budget_bucket = ?
                WHERE batch_id = ? AND api_called = 0
                """,
                (phase, bucket, batch_id),
            )
            if cursor.rowcount != 1:
                return BudgetReservation(False, "daily_call_limit", phase, bucket, "batch_already_reserved")
    except Exception:
        return BudgetReservation(False, "daily_call_limit", phase, bucket, "budget_bookkeeping_unavailable")
    return BudgetReservation(True, "reserved", phase, bucket)


def usage_summary(store: _BudgetStore | None, at: datetime | None = None) -> dict[str, object]:
    timestamp = at or datetime.now()
    strategy_usage = {key: 0 for key in sorted(_ALL_RESEARCH_STRATEGIES)}
    budget_usage = {key: 0 for key in strategy_limits()}
    window_usage = {key: 0 for key in window_limits()}
    result: dict[str, object] = {
        "daily_limit": daily_hard_limit(),
        "used": 0,
        "remaining": daily_hard_limit(),
        "usage_by_strategy": strategy_usage,
        "usage_by_budget": budget_usage,
        "usage_by_window": window_usage,
        "cache_hit_count": 0,
        "last_batch_id": "",
        "completed_at": "",
        "error_type": "",
        "error_message": "",
    }
    if store is None:
        return result
    try:
        with store.repository.connect() as conn:
            rows = conn.execute(
                """
                SELECT batch_id, strategy_name, status, api_called, call_phase, budget_bucket,
                       requested_at, completed_at, error_type, error_message, candidate_count
                FROM deepseek_analysis_batches
                WHERE substr(requested_at, 1, 10) = ?
                ORDER BY requested_at, batch_id
                """,
                (timestamp.date().isoformat(),),
            ).fetchall()
    except Exception:
        return result

    usage_by_strategy = dict(strategy_usage)
    usage_by_budget = dict(budget_usage)
    usage_by_window = dict(window_usage)
    called_rows = [row for row in rows if int(row[3] or 0) == 1]
    for row in called_rows:
        strategy = storage_strategy_name(row[1])
        phase = str(row[4] or "") or _phase_from_requested_at(row[6])
        bucket = str(row[5] or "") or _budget_bucket(strategy, phase)
        usage_by_strategy[strategy] = int(usage_by_strategy.get(strategy, 0)) + 1
        usage_by_budget[bucket] = int(usage_by_budget.get(bucket, 0)) + 1
        usage_by_window[phase] = int(usage_by_window.get(phase, 0)) + 1
    result["used"] = len(called_rows)
    result["remaining"] = max(0, daily_hard_limit() - len(called_rows))
    result["usage_by_strategy"] = usage_by_strategy
    result["usage_by_budget"] = usage_by_budget
    result["usage_by_window"] = usage_by_window
    result["cache_hit_count"] = sum(int(row[10] or 0) for row in rows if str(row[2] or "") == "cache_hit")
    if rows:
        latest = rows[-1]
        result.update(
            last_batch_id=str(latest[0] or ""),
            completed_at=str(latest[7] or ""),
            error_type=str(latest[8] or ""),
            error_message=str(latest[9] or ""),
        )
    return result


def latest_strategy_batch(
    store: _BudgetStore | None,
    strategy_name: str,
    at: datetime | None = None,
) -> dict[str, object]:
    if store is None:
        return {}
    timestamp = at or datetime.now()
    try:
        with store.repository.connect() as conn:
            row = conn.execute(
                """
                SELECT batch_id, status, candidate_count, valid_count, abstain_count,
                       completed_at, error_type, error_message
                FROM deepseek_analysis_batches
                WHERE strategy_name = ? AND substr(requested_at, 1, 10) = ?
                ORDER BY requested_at DESC, batch_id DESC LIMIT 1
                """,
                (storage_strategy_name(strategy_name), timestamp.date().isoformat()),
            ).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    return {
        "last_batch_id": str(row[0] or ""),
        "status": str(row[1] or ""),
        "requested": int(row[2] or 0),
        "reviewed": int(row[3] or 0),
        "abstain_count": int(row[4] or 0),
        "completed_at": str(row[5] or ""),
        "error_type": str(row[6] or ""),
        "error_message": str(row[7] or ""),
    }


def _phase_from_requested_at(value: object) -> str:
    try:
        return phase_at(datetime.fromisoformat(str(value).replace(" ", "T", 1)))
    except (TypeError, ValueError):
        return "closed"


def candidate_budget_priority(row: dict[str, object], index: int) -> tuple[int, int, float, int]:
    risk_fields: Iterable[object] = (
        row.get("announcement_flags"),
        row.get("event_risk_flags"),
        row.get("risk_words"),
        row.get("sell_risk"),
    )
    has_risk = any(bool(value) for value in risk_fields)
    has_evidence = bool(row.get("recent_news") or row.get("announcement_time") or row.get("policy_support_score"))
    raw_score = row.get("score")
    try:
        score = float(str(raw_score or 0.0))
    except ValueError:
        score = 0.0
    return (int(has_risk), int(has_evidence), score, -index)


__all__ = [
    "BudgetReservation",
    "candidate_budget_priority",
    "daily_hard_limit",
    "latest_strategy_batch",
    "phase_at",
    "reserve_api_call",
    "strategies_for_phase",
    "strategy_limits",
    "usage_summary",
    "window_limits",
]
