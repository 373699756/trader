from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.component.test_v2_persistence import _snapshot
from tests.unit.application.test_tomorrow_shadow import _shadow_snapshot_mock
from tests.unit.infra.test_tomorrow_decision_freezes import BOUNDARY, _decision, _freeze
from trader.application.ports.decision_freezes import DecisionFreezeConflictError
from trader.application.tomorrow_shadow_runtime import TomorrowShadowWorker
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.tomorrow_freeze import TomorrowFreezeCheckpoint
from trader.infra.persistence.snapshot_files import _matches_hash
from trader.infra.persistence.sqlite import SCHEMA_VERSION, connect, initialize_database
from trader.infra.persistence.tomorrow_decision_freezes import TomorrowDecisionFreezeRepository
from trader.infra.persistence.writer import SnapshotConflictError, SnapshotRepository

KILL_POINTS = (
    "manifest_staged",
    "payload_staged",
    "json_temporary_fsynced",
    "json_created",
    "directory_fsynced",
    "manifest_committed",
)


def test_schema_v9_migrates_recovery_payload_and_quarantine_audit(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    initialize_database(database)
    with connect(database) as connection:
        connection.execute("ALTER TABLE frozen_snapshots DROP COLUMN recovery_payload")
        connection.execute("ALTER TABLE frozen_snapshots DROP COLUMN recovery_sha256")
        connection.execute("DROP TABLE freeze_quarantine_audit")
        connection.execute(
            "UPDATE schema_meta SET value = '9' WHERE key = 'schema_version'",
        )

    initialize_database(database)

    with connect(database) as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(frozen_snapshots)")}
        version = int(connection.execute("SELECT value FROM schema_meta").fetchone()[0])
        audit_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'freeze_quarantine_audit'"
        ).fetchone()
    assert version == SCHEMA_VERSION
    assert {"recovery_payload", "recovery_sha256"} <= columns
    assert audit_table is not None


def test_tomorrow_legacy_manifests_migrate_to_explicit_recovery_states(tmp_path: Path) -> None:
    root = tmp_path / "tomorrow-v2"
    root.mkdir()
    database = root / "tomorrow-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tomorrow_freeze_checkpoints (
                trade_date TEXT PRIMARY KEY,
                version TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                boundary_at TEXT NOT NULL,
                decision_observed_at TEXT NOT NULL,
                decision_sequence INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'consumed')),
                consumed_at TEXT
            );
            CREATE TABLE tomorrow_decision_freezes (
                trade_date TEXT PRIMARY KEY,
                version TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                frozen_at TEXT NOT NULL,
                freeze_kind TEXT NOT NULL
            );
            """
        )

    TomorrowDecisionFreezeRepository(tmp_path).initialize()

    with sqlite3.connect(database) as connection:
        checkpoint_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'tomorrow_freeze_checkpoints'"
        ).fetchone()[0]
        freeze_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(tomorrow_decision_freezes)")}
    assert "'staged'" in checkpoint_sql
    assert {"status", "recovery_payload", "recovery_sha256", "error"} <= freeze_columns


@pytest.mark.parametrize("kill_point", KILL_POINTS)
def test_legacy_freeze_recovers_idempotently_from_every_kill_point(
    tmp_path: Path,
    kill_point: str,
) -> None:
    repository = SnapshotRepository(
        tmp_path,
        config_version="runtime-v2",
        fault_injector=_crash_at(kill_point),
    )
    repository.initialize()

    with pytest.raises(SimulatedCrash, match=kill_point):
        repository.freeze(_snapshot())
    with connect(tmp_path / "runtime.sqlite3") as connection:
        expected_digest = str(connection.execute("SELECT sha256 FROM frozen_snapshots").fetchone()[0])

    recovered = SnapshotRepository(tmp_path, config_version="runtime-v2")
    first = recovered.recover()
    second = recovered.recover()
    frozen = recovered.load_frozen(Strategy.TOMORROW, "2026-07-16")

    assert frozen is not None
    assert frozen.snapshot_id == "snapshot-1"
    assert first.recovered in {0, 1}
    assert second.recovered == 0
    path = next((tmp_path / "frozen").rglob("snapshot-1.json"))
    assert _matches_hash(path, expected_digest)
    with connect(tmp_path / "runtime.sqlite3") as connection:
        payload, recovery_hash, status = connection.execute(
            "SELECT recovery_payload, recovery_sha256, status FROM frozen_snapshots"
        ).fetchone()
    assert (payload, recovery_hash, status) == (None, "", "committed")


def test_corrupt_recovery_payload_releases_trade_date_without_reusing_bad_identity(tmp_path: Path) -> None:
    repository = SnapshotRepository(
        tmp_path,
        config_version="runtime-v2",
        fault_injector=_crash_at("payload_staged"),
    )
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())
    with connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "UPDATE frozen_snapshots SET recovery_payload = ?, recovery_sha256 = ?",
            (b"corrupt", "invalid"),
        )

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result.quarantined == 1
    with connect(tmp_path / "runtime.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM frozen_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM freeze_quarantine_audit").fetchone()[0] == 1
    replacement = replace(_snapshot(), snapshot_id="replacement")
    SnapshotRepository(tmp_path, config_version="runtime-v2").freeze(replacement)


def test_recovery_rejects_manifest_path_traversal_without_touching_external_file(tmp_path: Path) -> None:
    repository = SnapshotRepository(
        tmp_path,
        config_version="runtime-v2",
        fault_injector=_crash_at("payload_staged"),
    )
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())
    sentinel = tmp_path.parent / f"{tmp_path.name}-sentinel.json"
    sentinel.write_bytes(b"do-not-touch")
    with connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "UPDATE frozen_snapshots SET relative_path = ?",
            (f"../{sentinel.name}",),
        )

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result.quarantined == 1
    assert sentinel.read_bytes() == b"do-not-touch"
    with connect(tmp_path / "runtime.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM frozen_snapshots").fetchone()[0] == 0


def test_recovery_rejects_symlink_escape_without_touching_external_file(tmp_path: Path) -> None:
    repository = SnapshotRepository(
        tmp_path,
        config_version="runtime-v2",
        fault_injector=_crash_at("payload_staged"),
    )
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.freeze(_snapshot())
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    sentinel = external / "sentinel.json"
    sentinel.write_bytes(b"do-not-touch")
    link = tmp_path / "external-link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    with connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "UPDATE frozen_snapshots SET relative_path = ?",
            ("external-link/sentinel.json",),
        )

    result = SnapshotRepository(tmp_path, config_version="runtime-v2").recover()

    assert result.quarantined == 1
    assert sentinel.read_bytes() == b"do-not-touch"
    with connect(tmp_path / "runtime.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM frozen_snapshots").fetchone()[0] == 0


def test_committed_corruption_fails_closed_and_never_reopens_trade_date(tmp_path: Path) -> None:
    repository = SnapshotRepository(tmp_path, config_version="runtime-v2")
    repository.initialize()
    repository.freeze(_snapshot())
    path = next((tmp_path / "frozen").rglob("snapshot-1.json"))
    path.write_bytes(b"corrupt")

    result = repository.recover()

    assert result.quarantined == 1
    with connect(tmp_path / "runtime.sqlite3") as connection:
        row = connection.execute("SELECT status, error FROM frozen_snapshots").fetchone()
    assert row["status"] == "committed"
    assert row["error"]
    with pytest.raises(SnapshotConflictError, match="already frozen"):
        repository.freeze(replace(_snapshot(), snapshot_id="replacement"))


@pytest.mark.parametrize("kill_point", KILL_POINTS)
def test_tomorrow_freeze_recovers_same_version_and_hash_from_every_kill_point(
    tmp_path: Path,
    kill_point: str,
) -> None:
    frozen = _freeze(_decision(1, BOUNDARY))
    repository = TomorrowDecisionFreezeRepository(
        tmp_path,
        fault_injector=_crash_at(kill_point),
    )
    repository.initialize()

    with pytest.raises(SimulatedCrash, match=kill_point):
        repository.commit_freeze(frozen)

    recovered = TomorrowDecisionFreezeRepository(tmp_path)
    recovered.initialize()
    recovered.recover()
    recovered.recover()

    assert recovered.load_frozen(frozen.trade_date) == frozen
    database = tmp_path / "tomorrow-v2" / "tomorrow-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        payload, recovery_hash, status = connection.execute(
            "SELECT recovery_payload, recovery_sha256, status FROM tomorrow_decision_freezes"
        ).fetchone()
    assert (payload, recovery_hash, status) == (None, "", "committed")


@pytest.mark.parametrize("kill_point", KILL_POINTS)
def test_tomorrow_checkpoint_recovers_same_version_and_hash_from_every_kill_point(
    tmp_path: Path,
    kill_point: str,
) -> None:
    checkpoint = TomorrowFreezeCheckpoint(
        decision=_decision(1, BOUNDARY - timedelta(seconds=10)),
        boundary_at=BOUNDARY,
    )
    repository = TomorrowDecisionFreezeRepository(
        tmp_path,
        fault_injector=_crash_at(kill_point),
    )
    repository.initialize()

    with pytest.raises(SimulatedCrash, match=kill_point):
        repository.save_checkpoint(checkpoint)

    recovered = TomorrowDecisionFreezeRepository(tmp_path)
    recovered.initialize()
    recovered.recover()

    assert recovered.load_checkpoint(checkpoint.trade_date) == checkpoint
    database = tmp_path / "tomorrow-v2" / "tomorrow-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        payload, recovery_hash, status = connection.execute(
            "SELECT recovery_payload, recovery_sha256, status FROM tomorrow_freeze_checkpoints"
        ).fetchone()
    assert (payload, recovery_hash, status) == (None, "", "active")


def test_tomorrow_corrupt_staged_payload_is_audited_and_releases_identity(tmp_path: Path) -> None:
    checkpoint = TomorrowFreezeCheckpoint(
        decision=_decision(1, BOUNDARY - timedelta(seconds=10)),
        boundary_at=BOUNDARY,
    )
    repository = TomorrowDecisionFreezeRepository(
        tmp_path,
        fault_injector=_crash_at("payload_staged"),
    )
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.save_checkpoint(checkpoint)
    database = tmp_path / "tomorrow-v2" / "tomorrow-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE tomorrow_freeze_checkpoints
            SET recovery_payload = ?, recovery_sha256 = ?
            """,
            (b"corrupt", "invalid"),
        )

    recovered = TomorrowDecisionFreezeRepository(tmp_path)
    recovered.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tomorrow_freeze_checkpoints").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tomorrow_freeze_quarantine_audit").fetchone()[0] == 1
    recovered.save_checkpoint(
        TomorrowFreezeCheckpoint(
            decision=_decision(2, BOUNDARY - timedelta(seconds=5)),
            boundary_at=BOUNDARY,
        )
    )


def test_tomorrow_recovery_rejects_symlink_escape_without_touching_external_file(tmp_path: Path) -> None:
    frozen = _freeze(_decision(1, BOUNDARY))
    repository = TomorrowDecisionFreezeRepository(
        tmp_path,
        fault_injector=_crash_at("payload_staged"),
    )
    repository.initialize()
    with pytest.raises(SimulatedCrash):
        repository.commit_freeze(frozen)
    root = tmp_path / "tomorrow-v2"
    external = tmp_path.parent / f"{tmp_path.name}-tomorrow-external"
    external.mkdir()
    sentinel = external / "sentinel.json"
    sentinel.write_bytes(b"do-not-touch")
    link = root / "external-link"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    database = root / "tomorrow-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tomorrow_decision_freezes SET relative_path = ?",
            ("external-link/sentinel.json",),
        )

    TomorrowDecisionFreezeRepository(tmp_path).initialize()

    assert sentinel.read_bytes() == b"do-not-touch"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tomorrow_decision_freezes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM tomorrow_freeze_quarantine_audit").fetchone()[0] == 1


def test_tomorrow_committed_damage_remains_committed_and_conflicting(tmp_path: Path) -> None:
    frozen = _freeze(_decision(1, BOUNDARY))
    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()
    repository.commit_freeze(frozen)
    path = next((tmp_path / "tomorrow-v2" / "freezes").rglob("*.json"))
    path.write_bytes(b"corrupt")

    recovered = TomorrowDecisionFreezeRepository(tmp_path)
    recovered.initialize()
    database = tmp_path / "tomorrow-v2" / "tomorrow-v2.sqlite3"

    with sqlite3.connect(database) as connection:
        status, error = connection.execute("SELECT status, error FROM tomorrow_decision_freezes").fetchone()
    assert status == "committed"
    assert error
    with pytest.raises(DecisionFreezeConflictError, match="already committed"):
        recovered.commit_freeze(_freeze(_decision(2, BOUNDARY)))


def test_tomorrow_orphan_is_quarantined_with_audit(tmp_path: Path) -> None:
    orphan = tmp_path / "tomorrow-v2" / "freezes" / "2026-07-16" / "orphan.json"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"{}")

    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()

    database = tmp_path / "tomorrow-v2" / "tomorrow-v2.sqlite3"
    assert not orphan.exists()
    assert next((tmp_path / "tomorrow-v2" / "quarantine" / "orphans").rglob("orphan.json")).is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT reason FROM tomorrow_freeze_quarantine_audit").fetchone()[0] == (
            "orphan_without_manifest"
        )


def test_shadow_worker_retries_pending_sealed_freeze_without_another_baseline() -> None:
    retried = threading.Event()

    class RetryProcessor:
        def __init__(self) -> None:
            self.processed = 0
            self.retries = 0

        def process(self, _snapshot) -> bool:
            self.processed += 1
            return True

        def process_native(self, _native_input) -> bool:
            return True

        def retry_pending_freeze(self) -> bool:
            self.retries += 1
            if self.retries >= 2:
                retried.set()
                return True
            return False

    processor = RetryProcessor()
    worker = TomorrowShadowWorker(processor)
    worker.start()
    assert worker.offer(_shadow_snapshot_mock("sealed-baseline"))

    try:
        assert retried.wait(timeout=2.0)
    finally:
        worker.stop(wait=True, timeout_seconds=1.0)

    assert processor.processed == 1
    assert processor.retries == 2


def _crash_at(kill_point: str):
    def crash(stage: str) -> None:
        if stage == kill_point:
            raise SimulatedCrash(stage)

    return crash


class SimulatedCrash(RuntimeError):
    pass
