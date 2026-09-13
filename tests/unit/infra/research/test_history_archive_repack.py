from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import trader.infra.research.history_archive_repack as repack_module
import trader.infra.research.history_training_due as due_module
from trader.domain.research.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.domain.research.history_revision import HistoryRevision
from trader.infra.research.history_archive_repack import (
    HistoryArchiveRepackCoordinator,
    HistoryArchiveRepackFenceError,
    require_history_archive_repack_inactive,
)
from trader.infra.research.history_archive_repack_codec import read_tomorrow_training_memory_evidence
from trader.infra.research.history_archive_repack_state import (
    HistoryArchiveRepackRequirements,
)
from trader.infra.research.history_control_repository import SQLiteHistoryControlRepository
from trader.infra.research.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.infra.research.history_training_due import evaluate_history_training_due
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profiles.v3.training import run_repack_tomorrow_training
from trader.infra.scoring.profiles.v3.training_bundle_repository import ActiveTomorrowBundle
from trader.infra.scoring.profiles.v3.training_memory_evidence import TomorrowTrainingMemoryEvidence

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _write_training_memory_evidence(path: Path, training_input_hash: str) -> None:
    payload: dict[str, object] = {
        "schema_version": "tomorrow_training_memory_gate",
        "status": "passed",
        "training_status": "engineering_ready",
        "repeat_training_status": "already_current",
        "training_input_hash": training_input_hash,
        "model_hash": "b" * 64,
        "report_hash": "c" * 64,
        "peak_rss_bytes": 1024,
        "starting_peak_rss_bytes": 512,
        "max_rss_bytes": 2048 * 1024 * 1024,
        "sample_database_peak_bytes": 4096,
        "stage_durations_ms": {"partition_validation": 12.5, "model_fit": 7.0},
        "failure_reasons": [],
    }
    payload["content_hash"] = artifact_content_hash(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _side(day: date, adjustment: str, close: float) -> BaoStockDailySide:
    return BaoStockDailySide(
        "600001",
        day,
        adjustment,  # type: ignore[arg-type]
        close,
        close + 0.5,
        close - 0.5,
        close,
        100.0,
        1000.0,
        close - 0.1 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        0.02 if adjustment == "unadjusted" else None,
        "trading",
    )


def _revision(day: date, sequence: int, close: float) -> HistoryRevision:
    return HistoryRevision(
        sequence,
        "main",
        BaoStockDailyCell(
            "600001",
            day,
            "complete",
            _side(day, "unadjusted", close),
            _side(day, "qfq", close - 1.0),
        ),
        False,
        "bank",
        "sw",
    )


def _qfq_close(revision: HistoryRevision) -> float:
    assert revision.cell.qfq is not None
    close = revision.cell.qfq.close_price
    assert close is not None
    return close


def _archive(tmp_path: Path) -> tuple[Path, Path, HistoryActiveSnapshot]:
    source = tmp_path / "data/history/baostock"
    target = tmp_path / "data/historyless/baostock"
    month = source / "partitions/2026/09.sqlite3"
    repository = SQLiteHistoryMonthPartitionRepository(month, 2026, 9)
    repository.initialize()
    repository.save_revisions(
        (
            _revision(date(2026, 9, 9), 1, 10.0),
            _revision(date(2026, 9, 10), 1, 11.0),
            _revision(date(2026, 9, 10), 2, 12.0),
            _revision(date(2026, 9, 10), 3, 11.0),
        )
    )
    with sqlite3.connect(month) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA page_size=4096")
        connection.execute("VACUUM")
    reference = repository.seal()
    source_identity = HistorySourceIdentity("baostock", "baostock_daily", "python-sdk", NOW)
    calendar = HistoryCalendarIdentity((date(2026, 9, 9), date(2026, 9, 10)), source_identity.content_hash)
    universe = HistoryUniverseIdentity(
        (HistorySecurityIdentity("600001", "浦发银行", "main", date(1999, 11, 10), None),),
        source_identity.content_hash,
    )
    snapshot = HistoryActiveSnapshot(
        3,
        date(2026, 9, 10),
        date(2026, 9, 9),
        calendar.content_hash,
        universe.content_hash,
        source_identity.content_hash,
        (reference,),
    )
    control = SQLiteHistoryControlRepository(source / "control.sqlite3")
    control.initialize()
    control.save_source(source_identity)
    control.save_calendar(calendar)
    control.save_universe(universe)
    control.publish_snapshot(snapshot)
    return source, target, snapshot


def _requirements() -> HistoryArchiveRepackRequirements:
    return HistoryArchiveRepackRequirements(
        expected_partition_count=1,
        expected_trading_days=2,
        maximum_target_bytes=1024 * 1024,
        minimum_reduction_ratio=-1.0,
        reserve_bytes=0,
    )


def test_repack_build_is_resumable_and_preserves_revision_replay(tmp_path: Path) -> None:
    source, target, original = _archive(tmp_path)
    coordinator = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements())

    first = coordinator.build()
    repeated = coordinator.build()

    assert first == repeated
    assert first.state == "completed"
    assert first.target_snapshot_hash != original.content_hash
    with sqlite3.connect(target / "partitions/2026/09.sqlite3") as connection:
        assert connection.execute("PRAGMA page_size").fetchone() == (8192,)
        assert connection.execute("PRAGMA freelist_count").fetchone() == (0,)
    target_state = SQLiteHistoryControlRepository(target / "control.sqlite3").load_state()
    assert target_state.active_snapshot is not None
    assert target_state.active_snapshot.sequence == original.sequence + 1
    target_month = SQLiteHistoryMonthPartitionRepository(target / "partitions/2026/09.sqlite3", 2026, 9)
    assert _qfq_close(target_month.read_day(date(2026, 9, 10), snapshot_sequence=1)[0]) == 10.0
    assert _qfq_close(target_month.read_day(date(2026, 9, 10), snapshot_sequence=2)[0]) == 11.0
    assert _qfq_close(target_month.read_day(date(2026, 9, 10), snapshot_sequence=3)[0]) == 10.0


@pytest.mark.parametrize("failure_stage", ("partition_replaced:partitions/2026/09.sqlite3", "target_control_built"))
def test_repack_build_recovers_outputs_written_before_their_state_commit(tmp_path: Path, failure_stage: str) -> None:
    source, target, _original = _archive(tmp_path)

    def interrupt(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("simulated power loss")

    with pytest.raises(RuntimeError, match="power loss"):
        HistoryArchiveRepackCoordinator(
            source,
            target,
            requirements=_requirements(),
            fault_injector=interrupt,
        ).build()

    completed = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements()).build()

    assert completed.state == "completed"
    assert (target / "control.sqlite3").is_file()
    assert (target / "partitions/2026/09.sqlite3").is_file()


def test_activation_recovers_an_interrupted_move_and_can_retry_then_rollback(tmp_path: Path) -> None:
    source, target, original = _archive(tmp_path)
    HistoryArchiveRepackCoordinator(source, target, requirements=_requirements()).build()

    def interrupt(stage: str) -> None:
        if stage == "old_control_moved_moved":
            raise RuntimeError("simulated power loss")

    interrupted = HistoryArchiveRepackCoordinator(
        source,
        target,
        requirements=_requirements(),
        fault_injector=interrupt,
    )
    with pytest.raises(RuntimeError, match="power loss"):
        interrupted.activate()

    recovered = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements()).activate()
    assert recovered.state == "rolled_back"
    assert SQLiteHistoryControlRepository(source / "control.sqlite3").load_state().active_snapshot == original
    assert (target / "control.sqlite3").is_file()

    activated = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements()).activate()
    assert activated.state == "verified"
    with pytest.raises(HistoryArchiveRepackFenceError, match="activation_pending"):
        require_history_archive_repack_inactive(source)

    rolled_back = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements()).rollback()
    assert rolled_back.state == "rolled_back"
    assert SQLiteHistoryControlRepository(source / "control.sqlite3").load_state().active_snapshot == original
    require_history_archive_repack_inactive(source)


def test_fenced_training_may_proceed_only_for_the_exact_activated_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None

    expected = object()
    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training._run_tomorrow_training_locked",
        lambda *_args, **_kwargs: expected,
    )

    result = run_repack_tomorrow_training(
        source,
        tmp_path / "data/train",
        expected_history_snapshot_hash=built.target_snapshot_hash,
    )

    assert result is expected

    mismatch = run_repack_tomorrow_training(
        source,
        tmp_path / "data/train",
        expected_history_snapshot_hash="f" * 64,
    )
    assert mismatch.status == "blocked"
    assert mismatch.failure_reasons == ("history_archive_repack_activation_pending",)


def test_physical_repack_does_not_create_revision_due_or_invalidate_training_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, original = _archive(tmp_path)
    coordinator = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    contract_hash = "9" * 64
    bundle = ActiveTomorrowBundle(
        tmp_path / "data/train/tomorrow-v3/model.json",
        original.content_hash,
        original.source_identity_hash,
        original.label_cutoff,
        contract_hash,
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    monkeypatch.setattr(due_module, "_active_bundle", lambda _root: (bundle, False))

    evaluation = evaluate_history_training_due(
        source,
        tmp_path / "data/train",
        NOW,
        contract_hash,
    )

    assert built.target_snapshot_hash is not None
    assert evaluation is not None
    assert evaluation.active_snapshot.content_hash == built.target_snapshot_hash
    assert evaluation.state.reason == "training_contract_due"
    assert evaluation.state.training_due is True
    assert evaluation.state.input_revision is False
    assert evaluation.revised_dates == ()
    assert evaluation.invalidated_cache_dates == ()


def test_finalize_deletes_only_the_verified_backup_after_bundle_and_memory_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None
    monkeypatch.setattr(
        repack_module,
        "inspect_active_tomorrow_bundle",
        lambda _path: type(
            "Bundle",
            (),
            {
                "training_input_hash": built.target_snapshot_hash,
                "model_hash": "b" * 64,
                "report_hash": "c" * 64,
            },
        )(),
    )
    memory_evidence = tmp_path / "data/historyless/memory.json"
    _write_training_memory_evidence(memory_evidence, built.target_snapshot_hash)

    decoded = read_tomorrow_training_memory_evidence(memory_evidence)

    assert decoded.sample_database_peak_bytes == 4096
    assert tuple((item.stage, item.duration_ms) for item in decoded.stage_durations) == (
        ("model_fit", 7.0),
        ("partition_validation", 12.5),
    )

    finalized = coordinator.finalize(tmp_path / "data/train", memory_evidence)

    assert finalized.state == "finalized"
    assert finalized.released_bytes > 0
    assert not (tmp_path / "data/historyless/baostock-before-repack").exists()
    require_history_archive_repack_inactive(source)
    with pytest.raises(repack_module.HistoryArchiveRepackError, match="cannot be rolled back"):
        coordinator.rollback()


def test_finalize_refuses_to_delete_a_backup_with_unknown_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryArchiveRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None
    monkeypatch.setattr(
        repack_module,
        "inspect_active_tomorrow_bundle",
        lambda _path: type(
            "Bundle",
            (),
            {
                "training_input_hash": built.target_snapshot_hash,
                "model_hash": "b" * 64,
                "report_hash": "c" * 64,
            },
        )(),
    )
    monkeypatch.setattr(
        repack_module,
        "read_tomorrow_training_memory_evidence",
        lambda _path: TomorrowTrainingMemoryEvidence(
            "engineering_ready",
            "already_current",
            built.target_snapshot_hash,
            "b" * 64,
            "c" * 64,
            1024,
            2048 * 1024 * 1024,
            4096,
            (),
        ),
    )
    unexpected = tmp_path / "data/historyless/baostock-before-repack/partitions/unexpected.txt"
    unexpected.write_text("preserve me", encoding="utf-8")

    with pytest.raises(repack_module.HistoryArchiveRepackError, match="unknown file"):
        coordinator.finalize(tmp_path / "data/train", tmp_path / "data/historyless/memory.json")

    assert unexpected.read_text(encoding="utf-8") == "preserve me"
