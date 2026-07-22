"""Pipeline event and dependency observability persistence mixin."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.infra.persistence.sqlite import connection_scope
from trader.infra.persistence.writer_utils import (
    _event_integer,
    _non_negative_integer,
    _optional_number,
    _percentile,
    _safe_json_object,
    _safe_json_text,
)

FaultInjector = Callable[[str], None]


class RepositoryObservabilityMixin:
    _lock: Any
    _database_path: Path

    def reserve_event(self, event: Mapping[str, object]) -> bool:
        with self._lock, connection_scope(self._database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO pipeline_events(
                    event_id, event_type, subject_key, trade_date, phase, strategy,
                    priority, data_version, config_version, status, created_at,
                    deadline, retry_count, payload_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(event["event_id"]),
                    str(event["event_type"]),
                    str(event["subject_key"]),
                    str(event["trade_date"]),
                    str(event["phase"]),
                    str(event["strategy"]),
                    _event_integer(event, "priority"),
                    str(event["data_version"]),
                    str(event["config_version"]),
                    str(event["status"]),
                    str(event["created_at"]),
                    str(event.get("deadline") or ""),
                    _event_integer(event, "retry_count", default=0),
                    json.dumps(event.get("payload") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(event.get("error") or "")[:1000],
                ),
            )
            return cursor.rowcount == 1

    def compare_and_set_event(
        self,
        event_id: str,
        *,
        expected_status: str,
        status: str,
        retry_count: int,
        error: str = "",
    ) -> bool:
        with self._lock, connection_scope(self._database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE pipeline_events
                SET status = ?, retry_count = ?, error = ?
                WHERE event_id = ? AND status = ?
                """,
                (status, retry_count, error[:1000], event_id, expected_status),
            )
            return cursor.rowcount == 1

    def list_events(self, *, cursor: int, limit: int) -> Sequence[Mapping[str, object]]:
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM pipeline_events
                WHERE sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (max(0, cursor), max(1, limit)),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        )

    def pending_priority_events(self) -> Sequence[Mapping[str, object]]:
        with connection_scope(self._database_path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM pipeline_events
                WHERE status IN ('pending', 'running') AND priority <= 10
                ORDER BY sequence
                """
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        )

    def record_data_source_health(self, health: Mapping[str, object], *, updated_at: datetime) -> None:
        sources = health.get("sources")
        if not isinstance(sources, Mapping):
            return
        active_source = str(health.get("active_source") or "")
        route = health.get("route")
        if isinstance(route, Mapping):
            route_json = _safe_json_text(route)
            route_status = str(route.get("status") or "idle")
            route_fallback_reason = str(route.get("fallback_reason") or "")
            route_degraded = int(bool(route.get("degraded")))
        else:
            route_json = "{}"
            route_status = "idle"
            route_fallback_reason = ""
            route_degraded = 0
        market_age_summary = health.get("market_quote_age")
        market_age = (
            _optional_number(market_age_summary.get("maximum_seconds"))
            if isinstance(market_age_summary, Mapping)
            else None
        )
        candidate_age_summary = health.get("candidate_quote_age")
        candidate_age = (
            _optional_number(candidate_age_summary.get("maximum_seconds"))
            if isinstance(candidate_age_summary, Mapping)
            else None
        )
        with self._lock, connection_scope(self._database_path) as connection:
            for source, raw in sources.items():
                if not isinstance(raw, Mapping):
                    continue
                connection.execute(
                    """
                    INSERT INTO data_source_health(
                        source, planned_count, success_count, failure_count, circuit_open,
                        p50_latency_ms, p95_latency_ms, data_age_seconds, last_error, route_json,
                        route_status, route_fallback_reason, route_degraded, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source) DO UPDATE SET
                        planned_count = excluded.planned_count,
                        success_count = excluded.success_count,
                        failure_count = excluded.failure_count,
                        circuit_open = excluded.circuit_open,
                        p50_latency_ms = excluded.p50_latency_ms,
                        p95_latency_ms = excluded.p95_latency_ms,
                        data_age_seconds = excluded.data_age_seconds,
                        last_error = excluded.last_error,
                        route_json = excluded.route_json,
                        route_status = excluded.route_status,
                        route_fallback_reason = excluded.route_fallback_reason,
                        route_degraded = excluded.route_degraded,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(source)[:80],
                        _non_negative_integer(raw.get("planned_count")),
                        _non_negative_integer(raw.get("success_count")),
                        _non_negative_integer(raw.get("error_count")),
                        int(bool(raw.get("circuit_open"))),
                        _optional_number(raw.get("p50_latency_ms")),
                        _optional_number(raw.get("p95_latency_ms")),
                        candidate_age
                        if str(source) == "tencent"
                        else market_age
                        if str(source) == active_source
                        else None,
                        str(raw.get("last_error") or "")[:240],
                        route_json,
                        route_status,
                        route_fallback_reason,
                        route_degraded,
                        updated_at.isoformat(),
                    ),
                )

    def observability_status(self) -> Mapping[str, object]:
        try:
            with connection_scope(self._database_path) as connection:
                source_rows = connection.execute("SELECT * FROM data_source_health ORDER BY source").fetchall()
                call_rows = connection.execute(
                    """
                    SELECT outcome, http_status, error_code, latency_ms
                    FROM deepseek_calls ORDER BY requested_at DESC LIMIT 512
                    """
                ).fetchall()
                freeze_rows = connection.execute(
                    """
                    SELECT strategy, recommend_date, frozen_at, data_version, fusion_version,
                           sha256, anchor_json
                    FROM (
                        SELECT strategy, recommend_date, frozen_at, data_version, fusion_version,
                               sha256, anchor_json,
                               ROW_NUMBER() OVER (
                                   PARTITION BY strategy
                                   ORDER BY recommend_date DESC, frozen_at DESC
                               ) AS position
                        FROM frozen_snapshots
                        WHERE status = 'committed'
                    )
                    WHERE position = 1
                    ORDER BY strategy
                    """
                ).fetchall()
        except sqlite3.OperationalError:
            return {"data_sources": {}, "deepseek_calls": {}, "freezes": {}}
        latest_freezes = {
            str(row["strategy"]): {
                "trade_date": str(row["recommend_date"]),
                "frozen_at": str(row["frozen_at"]),
                "data_version": str(row["data_version"]),
                "fusion_version": str(row["fusion_version"]),
                "sha256": str(row["sha256"]),
                "anchors": _safe_json_object(str(row["anchor_json"])),
            }
            for row in freeze_rows
        }
        latencies = tuple(float(row["latency_ms"]) for row in call_rows if isinstance(row["latency_ms"], (int, float)))
        outcomes: dict[str, int] = {}
        for row in call_rows:
            outcome = str(row["outcome"])
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        return {
            "data_sources": {str(row["source"]): dict(row) for row in source_rows},
            "deepseek_calls": {
                "sample_size": len(call_rows),
                "outcomes": outcomes,
                "http_429_count": sum(row["http_status"] == 429 for row in call_rows),
                "timeout_count": sum(str(row["error_code"]) == "timeout" for row in call_rows),
                "p50_latency_ms": _percentile(latencies, 0.50),
                "p95_latency_ms": _percentile(latencies, 0.95),
            },
            "freezes": latest_freezes,
        }
