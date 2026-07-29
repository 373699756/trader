"""Durable, independently verifiable tomorrow shadow evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.application.tomorrow_shadow import (
    TomorrowCutoverGate,
    TomorrowCutoverPolicy,
    TomorrowShadowObservation,
)

EVIDENCE_SCHEMA_VERSION = "tomorrow_shadow_evidence_v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
_SQLITE_TIMEOUT_SECONDS = 0.05


class TomorrowShadowEvidenceUnavailableError(RuntimeError):
    """Raised when durable shadow evidence cannot be trusted."""


class TomorrowShadowEvidenceRepository:
    """Stores a bounded evidence window under the isolated tomorrow-v2 namespace."""

    def __init__(self, runtime_dir: Path, *, maximum_samples: int = 4096) -> None:
        if maximum_samples < 1:
            raise ValueError("tomorrow shadow evidence capacity must be positive")
        self._root = runtime_dir / "tomorrow-v2"
        self._database = self._root / "tomorrow-shadow-evidence.sqlite3"
        self._maximum_samples = maximum_samples
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                with self._connect() as connection:
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS tomorrow_shadow_evidence (
                            identity TEXT PRIMARY KEY,
                            trade_date TEXT NOT NULL,
                            observed_at TEXT NOT NULL,
                            payload BLOB NOT NULL,
                            payload_sha256 TEXT NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS tomorrow_shadow_evidence_observed
                        ON tomorrow_shadow_evidence(observed_at, identity);
                        """
                    )
            except (OSError, sqlite3.Error) as exc:
                raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence initialization failed") from exc

    def record(self, observation: TomorrowShadowObservation) -> None:
        payload = _observation_bytes(observation)
        payload_sha256 = _sha256(payload)
        identity = _observation_identity(observation)
        observed_at = observation.observed_at.isoformat()
        with self._lock:
            try:
                with self._connect() as connection:
                    current = connection.execute(
                        """
                        SELECT observed_at, payload_sha256
                        FROM tomorrow_shadow_evidence
                        WHERE identity = ?
                        """,
                        (identity,),
                    ).fetchone()
                    if current is not None and str(current[0]) >= observed_at:
                        if str(current[0]) == observed_at and str(current[1]) != payload_sha256:
                            raise TomorrowShadowEvidenceUnavailableError(
                                "tomorrow shadow evidence identity conflicts at the same observation time"
                            )
                        return
                    connection.execute(
                        """
                        INSERT INTO tomorrow_shadow_evidence (
                            identity, trade_date, observed_at, payload, payload_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(identity) DO UPDATE SET
                            trade_date = excluded.trade_date,
                            observed_at = excluded.observed_at,
                            payload = excluded.payload,
                            payload_sha256 = excluded.payload_sha256
                        WHERE excluded.observed_at > tomorrow_shadow_evidence.observed_at
                        """,
                        (
                            identity,
                            observation.trade_date.isoformat(),
                            observed_at,
                            payload,
                            payload_sha256,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM tomorrow_shadow_evidence
                        WHERE identity NOT IN (
                            SELECT identity
                            FROM tomorrow_shadow_evidence
                            ORDER BY observed_at DESC, identity DESC
                            LIMIT ?
                        )
                        """,
                        (self._maximum_samples,),
                    )
            except TomorrowShadowEvidenceUnavailableError:
                raise
            except sqlite3.Error as exc:
                raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence write failed") from exc

    def load_recent(self) -> tuple[TomorrowShadowObservation, ...]:
        with self._lock:
            if not self._database.is_file():
                raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence database does not exist")
            try:
                with self._connect_readonly() as connection:
                    rows = connection.execute(
                        """
                        SELECT identity, trade_date, observed_at, payload, payload_sha256
                        FROM tomorrow_shadow_evidence
                        ORDER BY observed_at DESC, identity DESC
                        LIMIT ?
                        """,
                        (self._maximum_samples,),
                    ).fetchall()
            except sqlite3.Error as exc:
                raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence read failed") from exc
        observations = tuple(self._verified_observation(row) for row in reversed(rows))
        return observations

    def build_report(self, policy: TomorrowCutoverPolicy | None = None) -> dict[str, object]:
        observations = self.load_recent()
        effective_policy = policy or TomorrowCutoverPolicy(maximum_samples=self._maximum_samples)
        if effective_policy.maximum_samples != self._maximum_samples:
            raise ValueError("tomorrow shadow report and repository capacities must match")
        gate = TomorrowCutoverGate(effective_policy)
        gate.restore(observations)
        payloads = [_observation_payload(observation) for observation in observations]
        evidence_hash = _sha256(_canonical_bytes(payloads))
        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "observation_count": len(observations),
            "first_observed_at": observations[0].observed_at.isoformat() if observations else None,
            "last_observed_at": observations[-1].observed_at.isoformat() if observations else None,
            "trade_dates": sorted({item.trade_date.isoformat() for item in observations}),
            "config_versions": sorted({item.config_version for item in observations}),
            "strategy_versions": sorted({item.strategy_version for item in observations}),
            "fusion_versions": sorted({item.fusion_version for item in observations}),
            "decision_schema_versions": sorted({item.decision_schema_version for item in observations}),
            "evidence_hash": evidence_hash,
            "cutover_status": asdict(gate.status()),
        }

    def _verified_observation(self, row: tuple[object, ...]) -> TomorrowShadowObservation:
        identity, trade_date_text, observed_at_text, stored_payload, stored_hash = row
        if isinstance(stored_payload, str):
            payload = stored_payload.encode("utf-8")
        elif isinstance(stored_payload, bytes):
            payload = stored_payload
        else:
            raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence payload type is invalid")
        if _sha256(payload) != str(stored_hash):
            raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence payload hash verification failed")
        try:
            observation = _observation_from_bytes(payload)
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise TomorrowShadowEvidenceUnavailableError("tomorrow shadow evidence payload validation failed") from exc
        if (
            _observation_identity(observation) != str(identity)
            or observation.trade_date.isoformat() != str(trade_date_text)
            or observation.observed_at.isoformat() != str(observed_at_text)
        ):
            raise TomorrowShadowEvidenceUnavailableError(
                "tomorrow shadow evidence manifest identity verification failed"
            )
        return observation

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database, timeout=_SQLITE_TIMEOUT_SECONDS)

    def _connect_readonly(self) -> sqlite3.Connection:
        return sqlite3.connect(
            f"{self._database.as_uri()}?mode=ro",
            uri=True,
            timeout=_SQLITE_TIMEOUT_SECONDS,
        )


def _observation_identity(observation: TomorrowShadowObservation) -> str:
    return _sha256(
        _canonical_bytes(
            {
                "trade_date": observation.trade_date.isoformat(),
                "baseline_snapshot_id": observation.baseline_snapshot_id,
                "input_version": observation.input_version,
            }
        )
    )


def _observation_bytes(observation: TomorrowShadowObservation) -> bytes:
    return _canonical_bytes(_observation_payload(observation))


def _observation_payload(observation: TomorrowShadowObservation) -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "trade_date": observation.trade_date.isoformat(),
        "observed_at": observation.observed_at.isoformat(),
        "baseline_snapshot_id": observation.baseline_snapshot_id,
        "decision_version": observation.decision_version,
        "input_version": observation.input_version,
        "config_version": observation.config_version,
        "strategy_version": observation.strategy_version,
        "fusion_version": observation.fusion_version,
        "decision_schema_version": observation.decision_schema_version,
        "parent_decision_version": observation.parent_decision_version,
        "selected_codes_match": observation.selected_codes_match,
        "filter_reasons_match": observation.filter_reasons_match,
        "local_publish_seconds": observation.local_publish_seconds,
        "decision_age_seconds": observation.decision_age_seconds,
        "processing_seconds": observation.processing_seconds,
        "deepseek_request_delta": observation.deepseek_request_delta,
        "resource_limits_passed": observation.resource_limits_passed,
        "baseline_frozen": observation.baseline_frozen,
        "v2_frozen": observation.v2_frozen,
        "freeze_codes_match": observation.freeze_codes_match,
        "freeze_content_hash": observation.freeze_content_hash,
        "processing_error": observation.processing_error,
    }


def _observation_from_bytes(payload: bytes) -> TomorrowShadowObservation:
    raw = json.loads(payload.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("tomorrow shadow evidence payload must be an object")
    data = cast(dict[str, object], raw)
    if _text(data, "schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("tomorrow shadow evidence schema is unsupported")
    return TomorrowShadowObservation(
        trade_date=date.fromisoformat(_text(data, "trade_date")),
        observed_at=_shanghai_datetime(_text(data, "observed_at")),
        baseline_snapshot_id=_text(data, "baseline_snapshot_id"),
        decision_version=_text(data, "decision_version"),
        input_version=_text(data, "input_version"),
        config_version=_text(data, "config_version"),
        strategy_version=_text(data, "strategy_version"),
        fusion_version=_text(data, "fusion_version"),
        decision_schema_version=_text(data, "decision_schema_version"),
        parent_decision_version=_text(data, "parent_decision_version", allow_empty=True),
        selected_codes_match=_boolean(data, "selected_codes_match"),
        filter_reasons_match=_boolean(data, "filter_reasons_match"),
        local_publish_seconds=_number(data, "local_publish_seconds"),
        decision_age_seconds=_number(data, "decision_age_seconds"),
        processing_seconds=_number(data, "processing_seconds"),
        deepseek_request_delta=_integer(data, "deepseek_request_delta"),
        resource_limits_passed=_boolean(data, "resource_limits_passed"),
        baseline_frozen=_boolean(data, "baseline_frozen"),
        v2_frozen=_boolean(data, "v2_frozen"),
        freeze_codes_match=_boolean(data, "freeze_codes_match"),
        freeze_content_hash=_text(data, "freeze_content_hash", allow_empty=True),
        processing_error=_text(data, "processing_error", allow_empty=True),
    )


def _text(data: dict[str, object], key: str, *, allow_empty: bool = False) -> str:
    value = data[key]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise TypeError(f"tomorrow shadow evidence {key} must be text")
    return value


def _boolean(data: dict[str, object], key: str) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        raise TypeError(f"tomorrow shadow evidence {key} must be boolean")
    return value


def _number(data: dict[str, object], key: str) -> float:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"tomorrow shadow evidence {key} must be numeric")
    return float(value)


def _integer(data: dict[str, object], key: str) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"tomorrow shadow evidence {key} must be an integer")
    return value


def _shanghai_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("tomorrow shadow evidence time must be timezone-aware")
    return parsed.astimezone(SHANGHAI)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "TomorrowShadowEvidenceRepository",
    "TomorrowShadowEvidenceUnavailableError",
]
