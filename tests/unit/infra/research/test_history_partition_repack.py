from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import trader.download.infra.history_partition_repack as repack_module
import trader.training.infra.history.history_training_due as due_module
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_reference import HistoryReferenceSnapshot, HistoryStEvidence
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_partition_repack import (
    HistoryPartitionRepackCoordinator,
    HistoryPartitionRepackFenceError,
    RequireHistoryPartitionRepackInactive,
)
from trader.download.infra.history_partition_repack_codec import ReadTomorrowTrainingMemoryEvidence
from trader.download.infra.history_partition_repack_state import (
    HistoryPartitionRepackRequirements,
)
from trader.infra.serialization.canonical import ContentHash
from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.history.history_training_due import EvaluateHistoryTrainingDue, HistoryTrainingDueQuery
from trader.training.infra.model_bundles.bundle_snapshot import ActiveHeadBundle
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.infra.profile.v3.training import RunRepackTomorrowTraining, RunRepackV3Training
from trader.training.infra.profile.v3.training_memory_evidence import TomorrowTrainingMemoryEvidence
from trader.training.infra.training_settings import LoadProfileTrainingSettings

NOW = datetime(2026, 9, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _write_training_memory_evidence(path: Path, training_input_hash: str) -> None:
    payload: dict[str, object] = {
        "schema_version": "tomorrow_training_memory_gate",
        "status": "passed",
        "training_status": "engineering_ready",
        "repeat_training_status": "engineering_ready",
        "training_input_hash": training_input_hash,
        "model_hash": "b" * 64,
        "report_hash": "c" * 64,
        "peak_rss_bytes": 1024,
        "starting_peak_rss_bytes": 512,
        "max_rss_bytes": 2048 * 1024 * 1024,
        "sample_database_peak_bytes": 4096,
        "stage_durations_ms": {"history_conversion": 12.5, "model_fit": 7.0},
        "failure_reasons": [],
    }
    payload["content_hash"] = ContentHash(payload)
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
    source = tmp_path / "data/history"
    target = tmp_path / "data/history-repacked"
    month = source / "prices_2026.sqlite3"
    repository = HistoryPricePartition(month, 2026, 9)
    repository.initialize()
    repository.save_revisions(
        (
            _revision(date(2026, 9, 9), 1, 10.0),
            _revision(date(2026, 9, 10), 1, 11.0),
            _revision(date(2026, 9, 10), 2, 12.0),
            _revision(date(2026, 9, 10), 3, 11.0),
        )
    )
    with closing(OpenSqliteConnection(month)) as connection, connection:
        connection.run("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.run("PRAGMA journal_mode=DELETE")
        connection.run("PRAGMA page_size=4096")
        connection.run("VACUUM")
    reference = repository.seal()
    bound_reference = HistoryReferenceSnapshot(("600001",), (), (HistoryStEvidence("600001", NOW.date(), "clear"),))
    source_identity = HistorySourceIdentity(
        "baostock", "baostock_daily", f"history_ref_{bound_reference.content_hash}_fixture", NOW
    )
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
    control = HistoryControlIndex(source / "catalog.sqlite3")
    control.initialize()
    control.save_source(source_identity)
    control.save_calendar(calendar)
    control.save_universe(universe)
    control.publish_snapshot(snapshot, reference=bound_reference)
    return source, target, snapshot


def _requirements() -> HistoryPartitionRepackRequirements:
    return HistoryPartitionRepackRequirements(
        expected_partition_count=1,
        expected_trading_days=2,
        maximum_target_bytes=1024 * 1024,
        minimum_reduction_ratio=-1.0,
        reserve_bytes=0,
    )


def test_repack_build_is_resumable_and_preserves_revision_replay(tmp_path: Path) -> None:
    source, target, original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())

    first = coordinator.build()
    repeated = coordinator.build()

    assert first == repeated
    assert first.state == "completed"
    assert first.target_snapshot_hash == original.content_hash
    with closing(OpenSqliteConnection(target / "prices_2026.sqlite3")) as connection, connection:
        assert tuple(connection.run("PRAGMA page_size").fetchone()) == (8192,)
        assert tuple(connection.run("PRAGMA freelist_count").fetchone()) == (0,)
    target_state = HistoryControlIndex(target / "catalog.sqlite3").load_state()
    assert target_state.active_snapshot is not None
    assert target_state.active_snapshot.sequence == original.sequence
    target_month = HistoryPricePartition(target / "prices_2026.sqlite3", 2026, 9)
    assert _qfq_close(target_month.read_day(date(2026, 9, 10), snapshot_sequence=1)[0]) == 10.0
    assert _qfq_close(target_month.read_day(date(2026, 9, 10), snapshot_sequence=2)[0]) == 11.0
    assert _qfq_close(target_month.read_day(date(2026, 9, 10), snapshot_sequence=3)[0]) == 10.0


@pytest.mark.parametrize("failure_stage", ("partition_replaced:prices_2026.sqlite3", "build_completed"))
def test_repack_build_recovers_outputs_written_before_their_state_commit(tmp_path: Path, failure_stage: str) -> None:
    source, target, _original = _archive(tmp_path)

    def interrupt(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("simulated power loss")

    with pytest.raises(RuntimeError, match="power loss"):
        HistoryPartitionRepackCoordinator(
            source,
            target,
            requirements=_requirements(),
            fault_injector=interrupt,
        ).build()

    completed = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements()).build()

    assert completed.state == "completed"
    assert (target / "catalog.sqlite3").is_file()
    assert (target / "prices_2026.sqlite3").is_file()


def test_activation_recovers_an_interrupted_move_and_can_retry_then_rollback(tmp_path: Path) -> None:
    source, target, original = _archive(tmp_path)
    HistoryPartitionRepackCoordinator(source, target, requirements=_requirements()).build()

    def interrupt(stage: str) -> None:
        if stage == "old_root_moved":
            raise RuntimeError("simulated power loss")

    interrupted = HistoryPartitionRepackCoordinator(
        source,
        target,
        requirements=_requirements(),
        fault_injector=interrupt,
    )
    with pytest.raises(RuntimeError, match="power loss"):
        interrupted.activate()

    recovered = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements()).activate()
    assert recovered.state == "rolled_back"
    assert HistoryControlIndex(source / "catalog.sqlite3").load_state().active_snapshot == original
    assert (target / "catalog.sqlite3").is_file()

    activated = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements()).activate()
    assert activated.state == "verified"
    with pytest.raises(HistoryPartitionRepackFenceError, match="activation_pending"):
        RequireHistoryPartitionRepackInactive(source)

    rolled_back = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements()).rollback()
    assert rolled_back.state == "rolled_back"
    assert HistoryControlIndex(source / "catalog.sqlite3").load_state().active_snapshot == original
    RequireHistoryPartitionRepackInactive(source)


def test_fenced_training_may_proceed_only_for_the_exact_activated_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None

    expected = object()
    monkeypatch.setattr(
        "trader.training.infra.engine._run_locked",
        lambda *_args, **_kwargs: SimpleNamespace(heads=(expected,)),
    )

    result = RunRepackTomorrowTraining(
        source,
        tmp_path / "data/training/models",
        settings=LoadProfileTrainingSettings(PROJECT_ROOT, "v3"),
        sample_root=tmp_path / "samples",
        expected_history_snapshot_hash=built.target_snapshot_hash,
    )

    assert result is expected

    mismatch = RunRepackTomorrowTraining(
        source,
        tmp_path / "data/training/models",
        settings=LoadProfileTrainingSettings(PROJECT_ROOT, "v3"),
        sample_root=tmp_path / "samples",
        expected_history_snapshot_hash="f" * 64,
    )
    assert mismatch.status == "blocked"
    assert mismatch.failure_reasons == ("history_archive_repack_activation_pending",)


def test_fenced_v3_training_uses_one_request_for_all_heads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None

    captured: list[tuple[Strategy, ...]] = []

    def locked(_archive: object, request: SimpleNamespace) -> object:
        captured.append(tuple(item.strategy for item in request.contracts))
        return SimpleNamespace(heads=())

    monkeypatch.setattr("trader.training.infra.engine._run_locked", locked)
    result = RunRepackV3Training(
        source,
        tmp_path / "data/training/models",
        settings=LoadProfileTrainingSettings(PROJECT_ROOT, "v3"),
        sample_root=tmp_path / "samples",
        expected_history_snapshot_hash=built.target_snapshot_hash,
    )

    assert result.heads == ()
    assert captured == [(Strategy.TOMORROW, Strategy.D25)]


def test_physical_repack_does_not_create_revision_due_or_invalidate_training_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    contract_hash = "9" * 64
    bundle = ActiveHeadBundle(
        tmp_path / "data/training/models/tomorrow-v3/model.json",
        Strategy.TOMORROW,
        original.content_hash,
        original.source_identity_hash,
        original.label_cutoff,
        contract_hash,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "e" * 64,
    )
    monkeypatch.setattr(due_module, "_active_bundle", lambda _root, _profile, _strategy: (bundle, False))

    evaluation = EvaluateHistoryTrainingDue(
        HistoryTrainingDueQuery(
            source,
            tmp_path / "data/training/models",
            NOW,
            V3_TRAINING_PROFILE,
            expected_training_contract_hash=contract_hash,
        )
    )

    assert built.target_snapshot_hash is not None
    assert evaluation is not None
    assert evaluation.active_snapshot.content_hash == built.target_snapshot_hash
    assert evaluation.state.reason == "not_due"
    assert evaluation.state.training_due is False
    assert evaluation.state.input_revision is False
    assert evaluation.revised_dates == ()
    assert evaluation.invalidated_cache_dates == ()


@pytest.mark.parametrize("interrupted", (False, True))
def test_finalize_deletes_only_the_verified_backup_after_bundle_and_memory_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None
    monkeypatch.setattr(
        repack_module,
        "InspectActiveProfileBundles",
        lambda _path, _profile: (
            type(
                "Bundle",
                (),
                {
                    "training_input_hash": built.target_snapshot_hash,
                    "model_hash": "b" * 64,
                    "report_hash": "c" * 64,
                },
            )(),
        ),
    )
    memory_evidence = tmp_path / "data/historyless/memory.json"
    _write_training_memory_evidence(memory_evidence, built.target_snapshot_hash)
    backup_partition = next((tmp_path / "data/history-before-repack").rglob("*.sqlite3"))
    Path(f"{backup_partition}-wal").touch()
    Path(f"{backup_partition}-shm").write_bytes(b"sqlite coordination state")

    decoded = ReadTomorrowTrainingMemoryEvidence(memory_evidence)

    assert decoded.sample_database_peak_bytes == 4096
    assert tuple((item.stage, item.duration_ms) for item in decoded.stage_durations) == (
        ("history_conversion", 12.5),
        ("model_fit", 7.0),
    )

    if interrupted:
        remove_tree = repack_module.shutil.rmtree

        def partial_delete(path):
            next(path.glob("prices_*.sqlite3")).unlink()
            raise OSError("interrupted backup cleanup")

        monkeypatch.setattr(repack_module.shutil, "rmtree", partial_delete)
        with pytest.raises(OSError, match="interrupted backup cleanup"):
            coordinator.finalize(tmp_path / "data/training/models", memory_evidence)
        with pytest.raises(repack_module.HistoryPartitionRepackError, match="cannot be rolled back"):
            coordinator.rollback()
        monkeypatch.setattr(repack_module.shutil, "rmtree", remove_tree)

    finalized = coordinator.finalize(tmp_path / "data/training/models", memory_evidence)

    assert finalized.state == "finalized"
    assert finalized.released_bytes > 0
    assert not (tmp_path / "data/history-before-repack").exists()
    RequireHistoryPartitionRepackInactive(source)
    with pytest.raises(repack_module.HistoryPartitionRepackError, match="cannot be rolled back"):
        coordinator.rollback()


def test_finalize_refuses_to_delete_a_backup_with_unknown_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None
    monkeypatch.setattr(
        repack_module,
        "InspectActiveProfileBundles",
        lambda _path, _profile: (
            type(
                "Bundle",
                (),
                {
                    "training_input_hash": built.target_snapshot_hash,
                    "model_hash": "b" * 64,
                    "report_hash": "c" * 64,
                },
            )(),
        ),
    )
    monkeypatch.setattr(
        repack_module,
        "ReadTomorrowTrainingMemoryEvidence",
        lambda _path: TomorrowTrainingMemoryEvidence(
            "engineering_ready",
            "engineering_ready",
            built.target_snapshot_hash,
            "b" * 64,
            "c" * 64,
            1024,
            2048 * 1024 * 1024,
            4096,
            (),
        ),
    )
    unexpected = tmp_path / "data/history-before-repack/unexpected.txt"
    unexpected.write_text("preserve me", encoding="utf-8")

    with pytest.raises(repack_module.HistoryPartitionRepackError, match="unexpected files"):
        coordinator.finalize(tmp_path / "data/training/models", tmp_path / "data/historyless/memory.json")

    assert unexpected.read_text(encoding="utf-8") == "preserve me"


def test_finalize_refuses_a_backup_partition_with_pending_wal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target, _original = _archive(tmp_path)
    coordinator = HistoryPartitionRepackCoordinator(source, target, requirements=_requirements())
    built = coordinator.build()
    coordinator.activate()
    assert built.target_snapshot_hash is not None
    monkeypatch.setattr(
        repack_module,
        "InspectActiveProfileBundles",
        lambda _path, _profile: (
            type(
                "Bundle",
                (),
                {
                    "training_input_hash": built.target_snapshot_hash,
                    "model_hash": "b" * 64,
                    "report_hash": "c" * 64,
                },
            )(),
        ),
    )
    monkeypatch.setattr(
        repack_module,
        "ReadTomorrowTrainingMemoryEvidence",
        lambda _path: TomorrowTrainingMemoryEvidence(
            "engineering_ready",
            "engineering_ready",
            built.target_snapshot_hash,
            "b" * 64,
            "c" * 64,
            1024,
            2048 * 1024 * 1024,
            4096,
            (),
        ),
    )
    backup_partition = next((tmp_path / "data/history-before-repack").rglob("*.sqlite3"))
    Path(f"{backup_partition}-wal").write_bytes(b"pending transaction")

    with pytest.raises(repack_module.HistoryPartitionRepackError, match="pending SQLite"):
        coordinator.finalize(tmp_path / "data/training/models", tmp_path / "data/historyless/memory.json")

    assert Path(f"{backup_partition}-wal").read_bytes() == b"pending transaction"
