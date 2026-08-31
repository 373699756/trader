"""Immutable SQLite store for all-candidate Tomorrow V1/V2 paired evidence."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from trader.application.ports.tomorrow_profile_comparison import TomorrowFormalPairTarget
from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.research.tomorrow_profile_comparison import (
    PairEvidenceState,
    TomorrowProfileComparisonReport,
    TomorrowProfileComparisonSpec,
    TomorrowProfileComparisonStatus,
    TomorrowProfilePairManifest,
)
from trader.infra.persistence.tomorrow_profile_comparison_codec import (
    manifest_bytes,
    manifest_from_bytes,
    outcome_bytes,
    outcome_from_bytes,
    report_bytes,
    report_identity_from_bytes,
)


class TomorrowProfileEvidenceConflictError(RuntimeError):
    pass


_SHANGHAI = ZoneInfo("Asia/Shanghai")


class SQLiteTomorrowProfileEvidenceStore:
    def __init__(self, runtime_root: Path, spec: TomorrowProfileComparisonSpec) -> None:
        self._database = runtime_root / "research" / "tomorrow-profile-comparison.sqlite3"
        self._spec = spec
        self._lock = threading.RLock()
        self._initialized = False
        self._status = self._empty_status(initialized=False)

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self._database.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript(_SCHEMA)
                row = connection.execute(
                    "SELECT spec_hash FROM comparison_specs WHERE research_identity = ?",
                    (self._spec.research_identity,),
                ).fetchone()
                if row is not None and str(row["spec_hash"]) != self._spec.content_hash:
                    raise TomorrowProfileEvidenceConflictError("immutable Tomorrow comparison spec conflict")
                if row is None:
                    connection.execute(
                        "INSERT INTO comparison_specs(research_identity, spec_hash, registered_on) VALUES (?, ?, ?)",
                        (self._spec.research_identity, self._spec.content_hash, self._spec.registered_on.isoformat()),
                    )
                self._status = self._status_from_connection(connection)
            self._initialized = True

    def save_manifest(self, manifest: TomorrowProfilePairManifest) -> None:
        if manifest.spec_hash != self._spec.content_hash:
            raise ValueError("Tomorrow profile manifest uses a different preregistration")
        payload = manifest_bytes(manifest)
        digest = _sha256(payload)
        self.initialize()
        with self._lock, self._connection() as connection:
            complete_days = self._qualified_day_count(connection)
            terminal = connection.execute("SELECT 1 FROM terminal_reports LIMIT 1").fetchone()
            if terminal is not None or complete_days >= self._spec.required_independent_days:
                return
            existing = connection.execute(
                "SELECT payload_hash, payload FROM prediction_manifests WHERE input_version = ?",
                (manifest.input_version,),
            ).fetchone()
            _raise_on_conflict(existing, payload, "prediction manifest")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO prediction_manifests(
                        input_version, trade_date, observed_at, common_candidate_count,
                        active_profile_id, payload_hash, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.input_version,
                        manifest.trade_date.isoformat(),
                        manifest.observed_at.isoformat(),
                        manifest.common_candidate_count,
                        manifest.active_profile_id,
                        digest,
                        payload,
                    ),
                )
            self._status = self._status_from_connection(connection)

    def load_manifest(self, input_version: str) -> TomorrowProfilePairManifest | None:
        self.initialize()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT payload_hash, payload FROM prediction_manifests WHERE input_version = ?",
                (input_version,),
            ).fetchone()
        if row is None:
            return None
        payload = bytes(row["payload"])
        if _sha256(payload) != str(row["payload_hash"]):
            raise TomorrowProfileEvidenceConflictError("Tomorrow prediction manifest hash mismatch")
        manifest = manifest_from_bytes(payload)
        if manifest.input_version != input_version or manifest.spec_hash != self._spec.content_hash:
            raise TomorrowProfileEvidenceConflictError("Tomorrow prediction manifest identity mismatch")
        return manifest

    def bind_formal_input(
        self,
        *,
        trade_date: date,
        input_version: str,
        record_version: str,
        committed_at: datetime,
    ) -> None:
        if committed_at.tzinfo is None or committed_at.astimezone(_SHANGHAI).date() != trade_date:
            raise ValueError("formal Tomorrow commit time must match its Shanghai trade date")
        manifest = self.load_manifest(input_version)
        if manifest is None or manifest.trade_date != trade_date:
            raise ValueError("formal Tomorrow input has no matching paired manifest")
        identity = f"{trade_date.isoformat()}|{input_version}|{record_version}|{committed_at.isoformat()}".encode()
        digest = _sha256(identity)
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT input_version, record_version, committed_at, identity_hash FROM formal_inputs WHERE trade_date = ?",
                (trade_date.isoformat(),),
            ).fetchone()
            if existing is not None:
                stored = (
                    str(existing["input_version"]),
                    str(existing["record_version"]),
                    str(existing["committed_at"]),
                    str(existing["identity_hash"]),
                )
                expected = (input_version, record_version, committed_at.isoformat(), digest)
                if stored != expected:
                    raise TomorrowProfileEvidenceConflictError("immutable formal Tomorrow input conflict")
            else:
                complete_days = self._qualified_day_count(connection)
                if complete_days >= self._spec.required_independent_days:
                    return
                connection.execute(
                    "INSERT INTO formal_inputs(trade_date, input_version, record_version, committed_at, identity_hash) VALUES (?, ?, ?, ?, ?)",
                    (trade_date.isoformat(), input_version, record_version, committed_at.isoformat(), digest),
                )
            connection.execute(
                "DELETE FROM prediction_manifests WHERE trade_date = ? AND input_version <> ?",
                (trade_date.isoformat(), input_version),
            )
            self._status = self._status_from_connection(connection)

    def pending_formal_targets(self, *, limit: int) -> Sequence[TomorrowFormalPairTarget]:
        if limit < 1:
            raise ValueError("Tomorrow profile target limit must be positive")
        self.initialize()
        with self._lock, self._connection() as connection:
            formal_rows = connection.execute(
                "SELECT trade_date, input_version, record_version FROM formal_inputs ORDER BY trade_date DESC"
            ).fetchall()
            settled = {
                (str(row["input_version"]), str(row["stock_code"]))
                for row in connection.execute("SELECT input_version, stock_code FROM pair_outcomes").fetchall()
            }
        targets: list[TomorrowFormalPairTarget] = []
        for row in formal_rows:
            input_version = str(row["input_version"])
            manifest = self.load_manifest(input_version)
            if manifest is None:
                raise TomorrowProfileEvidenceConflictError("formal Tomorrow input lost its paired manifest")
            for pair in manifest.pairs:
                if (input_version, pair.code) in settled:
                    continue
                targets.append(
                    TomorrowFormalPairTarget(
                        str(row["record_version"]),
                        input_version,
                        date.fromisoformat(str(row["trade_date"])),
                        pair,
                    )
                )
                if len(targets) >= limit:
                    return tuple(targets)
        return tuple(targets)

    def save_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None:
        self.initialize()
        with self._lock, self._connection() as connection:
            manifests: dict[str, TomorrowProfilePairManifest] = {}
            for outcome in outcomes:
                if outcome.horizon != 1 or outcome.strategy.value != "tomorrow":
                    raise ValueError("Tomorrow paired evidence only accepts T+1 outcomes")
                if outcome.settled_at.tzinfo is None:
                    raise ValueError("Tomorrow paired evidence settlement time must be timezone-aware")
                manifest = manifests.get(outcome.snapshot_id)
                if manifest is None:
                    manifest = self._formal_manifest_for_outcome(connection, outcome.snapshot_id)
                    manifests[outcome.snapshot_id] = manifest
                pairs = {pair.code: pair for pair in manifest.pairs}
                pair = pairs.get(outcome.stock_code)
                if pair is None:
                    raise ValueError("Tomorrow paired evidence stock code is outside the formal manifest")
                expected_atr = pair.atr20_pct if pair.atr20_pct is not None else 0.0
                if (
                    outcome.recommend_date != manifest.trade_date.isoformat()
                    or outcome.anchor_price != pair.anchor_price
                    or outcome.atr20_pct != expected_atr
                ):
                    raise ValueError("Tomorrow paired outcome does not match its formal prediction identity")
                payload = outcome_bytes(outcome)
                existing = connection.execute(
                    "SELECT payload_hash, payload FROM pair_outcomes WHERE input_version = ? AND stock_code = ?",
                    (outcome.snapshot_id, outcome.stock_code),
                ).fetchone()
                _raise_on_conflict(existing, payload, "pair outcome")
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO pair_outcomes(
                            input_version, stock_code, recommend_date, status, settled_at, payload_hash, payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            outcome.snapshot_id,
                            outcome.stock_code,
                            outcome.recommend_date,
                            outcome.status,
                            outcome.settled_at.isoformat(),
                            _sha256(payload),
                            payload,
                        ),
                    )
            self._status = self._status_from_connection(connection)

    def _formal_manifest_for_outcome(
        self,
        connection: sqlite3.Connection,
        input_version: str,
    ) -> TomorrowProfilePairManifest:
        row = connection.execute(
            """
            SELECT prediction_manifests.payload_hash, prediction_manifests.payload
            FROM formal_inputs
            JOIN prediction_manifests USING(input_version)
            WHERE formal_inputs.input_version = ?
            """,
            (input_version,),
        ).fetchone()
        if row is None:
            raise ValueError("Tomorrow paired outcome input is not formally bound")
        payload = bytes(row["payload"])
        if _sha256(payload) != str(row["payload_hash"]):
            raise TomorrowProfileEvidenceConflictError("formal Tomorrow manifest hash mismatch")
        manifest = manifest_from_bytes(payload)
        if manifest.input_version != input_version or manifest.spec_hash != self._spec.content_hash:
            raise TomorrowProfileEvidenceConflictError("formal Tomorrow manifest identity mismatch")
        return manifest

    def settled_outcomes(self) -> Sequence[RecommendationOutcome]:
        self.initialize()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_hash, payload FROM pair_outcomes ORDER BY recommend_date, stock_code"
            ).fetchall()
        values: list[RecommendationOutcome] = []
        for row in rows:
            payload = bytes(row["payload"])
            if _sha256(payload) != str(row["payload_hash"]):
                raise TomorrowProfileEvidenceConflictError("Tomorrow pair outcome hash mismatch")
            values.append(outcome_from_bytes(payload))
        return tuple(values)

    def complete_outcomes(self) -> Sequence[RecommendationOutcome]:
        return tuple(item for item in self.settled_outcomes() if item.status == "complete")

    def formal_manifests(self) -> Sequence[TomorrowProfilePairManifest]:
        self.initialize()
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT input_version FROM formal_inputs ORDER BY trade_date").fetchall()
        manifests = tuple(self.load_manifest(str(row["input_version"])) for row in rows)
        if any(item is None for item in manifests):
            raise TomorrowProfileEvidenceConflictError("formal Tomorrow manifest is unavailable")
        return tuple(item for item in manifests if item is not None)

    def status(self) -> TomorrowProfileComparisonStatus:
        """Return the last background-updated snapshot without touching SQLite."""

        with self._lock:
            return self._status

    def mark_unavailable(self, error_code: str) -> None:
        if not error_code or len(error_code) > 64:
            raise ValueError("Tomorrow profile evidence error code is invalid")
        with self._lock:
            self._status = self._empty_status(initialized=False, error_code=error_code)

    def inspect_status(self) -> TomorrowProfileComparisonStatus:
        """Read persistent status for explicit offline diagnostics without creating state."""

        if not self._database.is_file():
            return self._empty_status(initialized=False)
        try:
            target = f"file:{self._database.resolve()}?mode=ro"
            with sqlite3.connect(target, timeout=5.0, uri=True) as connection:
                return self._status_from_connection(connection)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            return self._empty_status(initialized=True, error_code=type(exc).__name__)

    def inspect_terminal_report_bytes(self) -> bytes | None:
        """Read and verify the sealed report without initializing or mutating the store."""

        if not self._database.is_file():
            return None
        try:
            target = f"file:{self._database.resolve()}?mode=ro"
            with sqlite3.connect(target, timeout=5.0, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT spec_hash, report_hash, payload_hash, payload FROM terminal_reports LIMIT 1"
                ).fetchone()
            if row is None:
                return None
            payload = bytes(row["payload"])
            spec_hash, report_hash = report_identity_from_bytes(payload)
            if (
                str(row["spec_hash"]) != self._spec.content_hash
                or str(row["payload_hash"]) != _sha256(payload)
                or spec_hash != self._spec.content_hash
                or report_hash != str(row["report_hash"])
            ):
                raise TomorrowProfileEvidenceConflictError("Tomorrow terminal report identity mismatch")
            return payload
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise TomorrowProfileEvidenceConflictError("Tomorrow terminal report is invalid") from exc

    def save_terminal_report(self, report: TomorrowProfileComparisonReport) -> None:
        if report.spec_hash != self._spec.content_hash or report.state not in {"review_ready", "rejected"}:
            raise ValueError("Tomorrow profile terminal report is not eligible for sealing")
        payload = report_bytes(report)
        self.initialize()
        with self._lock, self._connection() as connection:
            existing = connection.execute("SELECT payload_hash, payload FROM terminal_reports LIMIT 1").fetchone()
            _raise_on_conflict(existing, payload, "terminal report")
            if existing is None:
                connection.execute(
                    "INSERT INTO terminal_reports(spec_hash, state, report_hash, payload_hash, payload) VALUES (?, ?, ?, ?, ?)",
                    (self._spec.content_hash, report.state, report.content_hash, _sha256(payload), payload),
                )
            self._status = self._status_from_connection(connection)

    def _empty_status(
        self,
        *,
        initialized: bool,
        error_code: str = "",
    ) -> TomorrowProfileComparisonStatus:
        return TomorrowProfileComparisonStatus(
            initialized,
            self._spec.content_hash,
            0,
            0,
            0,
            0,
            0,
            0,
            self._spec.required_independent_days,
            self._spec.minimum_paired_candidates,
            "collecting",
            None,
            None,
            error_code=error_code,
        )

    def _status_from_connection(self, connection: sqlite3.Connection) -> TomorrowProfileComparisonStatus:
        manifests = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(common_candidate_count), 0), MAX(trade_date) FROM prediction_manifests"
        ).fetchone()
        formal = connection.execute("SELECT COUNT(*) FROM formal_inputs").fetchone()
        outcomes = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(status = 'complete'), 0),
                   COUNT(DISTINCT CASE WHEN status = 'complete' THEN recommend_date END),
                   MAX(CASE WHEN status = 'complete' THEN settled_at END)
            FROM pair_outcomes
            """
        ).fetchone()
        terminal = connection.execute("SELECT state FROM terminal_reports LIMIT 1").fetchone()
        days = self._qualified_day_count(connection)
        complete = int(outcomes[1])
        state = cast(
            PairEvidenceState,
            str(terminal[0])
            if terminal is not None
            else (
                "power_ready"
                if days >= self._spec.required_independent_days and complete >= self._spec.minimum_paired_candidates
                else "collecting"
            ),
        )
        return TomorrowProfileComparisonStatus(
            True,
            self._spec.content_hash,
            int(manifests[0]),
            int(manifests[1]),
            int(formal[0]),
            int(outcomes[0]),
            complete,
            days,
            self._spec.required_independent_days,
            self._spec.minimum_paired_candidates,
            state,
            date.fromisoformat(str(manifests[2])) if manifests[2] is not None else None,
            datetime.fromisoformat(str(outcomes[3])).date() if outcomes[3] is not None else None,
        )

    def _qualified_day_count(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT formal_inputs.trade_date
                FROM formal_inputs
                JOIN prediction_manifests USING(input_version)
                LEFT JOIN pair_outcomes USING(input_version)
                GROUP BY formal_inputs.trade_date, prediction_manifests.common_candidate_count
                HAVING COUNT(pair_outcomes.stock_code) = prediction_manifests.common_candidate_count
                   AND SUM(CASE WHEN pair_outcomes.status = 'complete' THEN 1 ELSE 0 END) >= ?
            ) AS qualified_days
            """,
            (self._spec.minimum_paired_candidates,),
        ).fetchone()
        return int(row[0])

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _raise_on_conflict(existing: sqlite3.Row | None, payload: bytes, label: str) -> None:
    if existing is None:
        return
    if str(existing["payload_hash"]) != _sha256(payload) or bytes(existing["payload"]) != payload:
        raise TomorrowProfileEvidenceConflictError(f"immutable Tomorrow {label} conflict")


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS comparison_specs(
    research_identity TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL,
    registered_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prediction_manifests(
    input_version TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    common_candidate_count INTEGER NOT NULL,
    active_profile_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS prediction_manifests_date_idx ON prediction_manifests(trade_date, observed_at);
CREATE TABLE IF NOT EXISTS formal_inputs(
    trade_date TEXT PRIMARY KEY,
    input_version TEXT NOT NULL UNIQUE,
    record_version TEXT NOT NULL UNIQUE,
    committed_at TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    FOREIGN KEY(input_version) REFERENCES prediction_manifests(input_version)
);
CREATE TABLE IF NOT EXISTS pair_outcomes(
    input_version TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    recommend_date TEXT NOT NULL,
    status TEXT NOT NULL,
    settled_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL,
    PRIMARY KEY(input_version, stock_code),
    FOREIGN KEY(input_version) REFERENCES prediction_manifests(input_version)
);
CREATE INDEX IF NOT EXISTS pair_outcomes_date_idx ON pair_outcomes(recommend_date, status);
CREATE TABLE IF NOT EXISTS terminal_reports(
    spec_hash TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload BLOB NOT NULL
);
"""


__all__ = [
    "SQLiteTomorrowProfileEvidenceStore",
    "TomorrowProfileEvidenceConflictError",
]
