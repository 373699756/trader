from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scripts import convert_baostock_history as converter
from trader.infra.research.baostock_gap_supplier import BaoStockGapRecord, BaoStockGapResult
from trader.infra.research.history_control_repository import HistoryControlError, SQLiteHistoryControlRepository
from trader.infra.research.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.infra.research.history_training_input import SQLiteHistoryTrainingInputArchive


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _side(code: str, day: str, adjustment: str, close: float) -> dict[str, object]:
    return {
        "adjustment": adjustment,
        "amount": 1000.0,
        "close_price": close,
        "code": code,
        "high_price": close + 0.2,
        "low_price": close - 0.2,
        "open_price": close - 0.1,
        "pct_change": 0.01 if adjustment == "unadjusted" else None,
        "preclose": close - 0.1 if adjustment == "unadjusted" else None,
        "trade_date": day,
        "trading_status": "trading",
        "turnover": 0.02 if adjustment == "unadjusted" else None,
        "volume": 100.0,
    }


def _cell(code: str, day: str, raw_close: float, qfq_close: float) -> str:
    return _json(
        {
            "code": code,
            "qfq": _side(code, day, "qfq", qfq_close),
            "status": "complete",
            "trade_date": day,
            "unadjusted": _side(code, day, "unadjusted", raw_close),
        }
    )


def _create_parent(root: Path) -> Path:
    shard = root / "shards/main-6000.sqlite3"
    shard.parent.mkdir(parents=True)
    calendar = {
        "open_dates": ["2026-08-31", "2026-09-01"],
        "schema_version": "baostock_exchange_calendar",
    }
    universe = [
        {
            "board": "main",
            "code": "600001",
            "delisted_on": None,
            "listed_on": "2020-01-01",
            "name": "fixture",
            "source_version": "fixture",
        }
    ]
    with sqlite3.connect(shard) as connection:
        connection.executescript(
            """
            CREATE TABLE context (
                singleton INTEGER PRIMARY KEY,
                spec_json TEXT NOT NULL,
                calendar_json TEXT NOT NULL,
                universe_json TEXT NOT NULL,
                versions_json TEXT NOT NULL,
                context_hash TEXT NOT NULL
            );
            CREATE TABLE daily_cells (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, trade_date)
            );
            CREATE TABLE daily_facts (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                is_st INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, trade_date)
            );
            CREATE TABLE industry_intervals (
                code TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                industry TEXT NOT NULL,
                classification TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, effective_from)
            );
            CREATE INDEX daily_cells_trade_date_idx ON daily_cells(trade_date, code);
            """
        )
        connection.execute(
            "INSERT INTO context VALUES (1, ?, ?, ?, ?, ?)",
            (
                _json(
                    {
                        "point_in_time_parity": False,
                        "production_authority": False,
                        "research_identity": "baostock_daily_core",
                        "schema_version": "baostock_daily_core",
                        "sessions": 2,
                        "source_cutoff": "2026-09-01",
                    }
                ),
                _json(calendar),
                _json(universe),
                _json(
                    {
                        "dependency_versions": [],
                        "python_version": "3.14",
                        "sdk_version": "fixture",
                    }
                ),
                "0" * 64,
            ),
        )
        for day, raw_close, qfq_close in (
            ("2026-08-31", 10.0, 9.0),
            ("2026-09-01", 11.0, 10.0),
        ):
            payload = _cell("600001", day, raw_close, qfq_close)
            connection.execute(
                "INSERT INTO daily_cells VALUES (?, ?, ?, ?)",
                ("600001", day, payload, hashlib.sha256(payload.encode()).hexdigest()),
            )
            connection.execute(
                "INSERT INTO daily_facts VALUES (?, ?, ?, ?)",
                ("600001", day, 0, hashlib.sha256(f"st:{day}".encode()).hexdigest()),
            )
        connection.execute(
            "INSERT INTO industry_intervals VALUES (?, ?, ?, ?, ?, ?)",
            ("600001", "2026-08-31", None, "bank", "fixture", "1" * 64),
        )
    return shard


def _create_increment(root: Path) -> tuple[Path, Path]:
    increment_root = root / "increments/fixture"
    shard = increment_root / "shards/increment-main-6000.sqlite3"
    shard.parent.mkdir(parents=True)
    with sqlite3.connect(shard) as connection:
        connection.executescript(
            """
            CREATE TABLE archive_context (
                singleton INTEGER PRIMARY KEY,
                context_hash TEXT NOT NULL
            );
            CREATE TABLE records (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                field_family TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (code, trade_date, field_family)
            );
            CREATE TABLE checkpoints (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                field_family TEXT NOT NULL,
                state TEXT NOT NULL,
                error_code TEXT,
                content_hash TEXT,
                PRIMARY KEY (code, trade_date, field_family)
            );
            """
        )
        connection.execute("INSERT INTO archive_context VALUES (1, ?)", ("2" * 64,))
        records = (
            ("600001", "2026-09-01", "daily_qfq", _json(_side("600001", "2026-09-01", "qfq", 10.5))),
            ("600001", "2026-09-02", "daily_raw", _json(_side("600001", "2026-09-02", "unadjusted", 12.0))),
            ("600001", "2026-09-02", "daily_qfq", _json(_side("600001", "2026-09-02", "qfq", 11.0))),
            ("600001", "2026-09-02", "is_st", _json({"code": "600001", "is_st": True, "trade_date": "2026-09-02"})),
            (
                "600001",
                "2026-09-02",
                "industry",
                _json(
                    {
                        "classification": "fixture",
                        "code": "600001",
                        "effective_to": None,
                        "industry": "finance",
                        "trade_date": "2026-09-02",
                    }
                ),
            ),
        )
        for code, day, family, payload in records:
            content_hash = hashlib.sha256(payload.encode()).hexdigest()
            connection.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?)",
                (code, day, family, payload, content_hash),
            )
            connection.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, 'completed', NULL, ?)",
                (code, day, family, content_hash),
            )
    manifest_path = increment_root / "manifest.json"
    manifest_path.write_text(
        _json(
            {
                "partitions": [
                    {
                        "checkpoint_count": 5,
                        "record_count": 5,
                        "relative_path": "shards/increment-main-6000.sqlite3",
                        "schema_version": "baostock_increment_partition",
                        "sha256": _sha256(shard),
                    }
                ],
                "schema_version": "baostock_increment_manifest",
                "source_cutoff": "2026-09-02",
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, shard


def _create_source(root: Path) -> None:
    root.mkdir(parents=True)
    parent_shard = _create_parent(root)
    increment_manifest, _increment_shard = _create_increment(root)
    manifest = {
        "audit": {"status": "historical_data_insufficient"},
        "calendar_hash": "3" * 64,
        "content_hash": "4" * 64,
        "partitions": [
            {
                "board": "main",
                "code_prefix": "6000",
                "codes": ["600001"],
                "database_sha256": _sha256(parent_shard),
                "logical_records_hash": "5" * 64,
                "relative_path": "shards/main-6000.sqlite3",
                "row_count": 2,
                "schema_version": "baostock_partition_ref",
            }
        ],
        "point_in_time_parity": False,
        "production_authority": False,
        "schema_version": "baostock_daily_manifest",
        "source_versions": {"sdk_version": "fixture"},
        "source_versions_hash": "6" * 64,
        "universe_hash": "7" * 64,
    }
    (root / "manifest.json").write_text(_json(manifest), encoding="utf-8")
    increment = json.loads(increment_manifest.read_text(encoding="utf-8"))
    increment.update(
        {
            "calendar_hash": "9" * 64,
            "content_hash": "b" * 64,
            "parent_manifest_file_hash": _sha256(root / "manifest.json"),
            "parent_manifest_hash": "4" * 64,
            "point_in_time_parity": False,
            "production_authority": False,
            "source_identity_hash": "c" * 64,
        }
    )
    increment_manifest.write_text(_json(increment), encoding="utf-8")
    active = {
        "active_data_hash": "8" * 64,
        "calendar_hash": "9" * 64,
        "content_hash": "a" * 64,
        "field_coverage": [
            {
                "family": "daily_raw",
                "incremental_rows": 1,
                "missing_reason": None,
                "missing_rows": 0,
                "reusable_rows": 2,
            },
            {
                "family": "qualification",
                "incremental_rows": 0,
                "missing_reason": "historical_effective_at_source_unavailable",
                "missing_rows": 3,
                "reusable_rows": 0,
            },
        ],
        "increment_manifest_hash": "b" * 64,
        "increment_manifest_path": str(increment_manifest.relative_to(root)),
        "parent_manifest_file_hash": _sha256(root / "manifest.json"),
        "parent_manifest_hash": "4" * 64,
        "point_in_time_parity": False,
        "production_authority": False,
        "schema_version": "baostock_active_manifest",
        "source_cutoff": "2026-09-02",
        "source_identity_hash": "c" * 64,
    }
    (root / "active-manifest.json").write_text(_json(active), encoding="utf-8")
    (root / ".download.lock").touch()


def _remove_increment_qfq(root: Path) -> None:
    increment = root / "increments/fixture/shards/increment-main-6000.sqlite3"
    with sqlite3.connect(increment) as connection:
        connection.execute(
            "DELETE FROM records WHERE code='600001' AND trade_date='2026-09-02' AND field_family='daily_qfq'"
        )
        connection.execute(
            "DELETE FROM checkpoints WHERE code='600001' AND trade_date='2026-09-02' AND field_family='daily_qfq'"
        )
    increment_manifest = root / "increments/fixture/manifest.json"
    manifest = json.loads(increment_manifest.read_text(encoding="utf-8"))
    manifest["partitions"][0]["record_count"] = 4
    manifest["partitions"][0]["checkpoint_count"] = 4
    manifest["partitions"][0]["sha256"] = _sha256(increment)
    increment_manifest.write_text(_json(manifest), encoding="utf-8")


def _file_hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): _sha256(path) for path in sorted(root.rglob("*")) if path.is_file()}


def _rewrite_completed_target_as_hash_layout(target: Path) -> tuple[Path, Path]:
    control = target / "control.sqlite3"
    with sqlite3.connect(control) as connection:
        record_key, payload_json = connection.execute(
            "SELECT record_key, payload_json FROM immutable_records WHERE kind='snapshot'"
        ).fetchone()
        payload = json.loads(payload_json)
        partition = payload["partitions"][0]
        stable = target / partition["relative_path"]
        hashed = stable.with_suffix("") / f"{partition['sha256']}.sqlite3"
        hashed.parent.mkdir(parents=True)
        stable.replace(hashed)
        partition["relative_path"] = str(hashed.relative_to(target))
        legacy_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
        connection.execute(
            "UPDATE immutable_records SET content_hash=?, payload_json=? WHERE kind='snapshot' AND record_key=?",
            (legacy_hash, _json(payload), record_key),
        )
        connection.execute("UPDATE active_snapshot SET snapshot_hash=? WHERE singleton=1", (legacy_hash,))
    return stable, hashed


def test_converter_streams_parent_and_active_increment_into_month_partitions(tmp_path: Path) -> None:
    source = tmp_path / "baostock-daily/sessions-2000"
    target = tmp_path / "baostock"
    _create_source(source)
    before = _file_hashes(source)

    summary = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )

    assert summary.state == "completed"
    assert summary.partition_count == 1
    assert summary.physical_rows == 3
    assert summary.active_rows == 2
    assert summary.active_calendar_days == 2
    assert summary.data_cutoff == "2026-09-02"
    assert _file_hashes(source) == before
    assert not target.with_name(".baostock-conversion").exists()

    september_files = (target / "partitions/2026/09.sqlite3",)
    assert not (target / "partitions/2026/08").exists()
    assert len(september_files) == 1
    september = september_files[0]
    assert september.is_file()
    with sqlite3.connect(september) as connection:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_records'"
        ).fetchone()[0]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='daily_records'"
            )
        }
        assert "PRIMARY KEY (trade_date, code, revision_id)" in schema
        assert "WITHOUT ROWID" in schema
        assert "history_month_code_date_idx" in indexes
        assert "history_month_date_board_code_idx" in indexes
        revisions = connection.execute(
            "SELECT first_seen_sequence, payload_json FROM daily_records "
            "WHERE code='600001' AND trade_date='2026-09-01' ORDER BY first_seen_sequence"
        ).fetchall()
        assert len(revisions) == 2
        assert json.loads(revisions[0][1])["cell"]["qfq"]["close_price"] == 10.0
        assert json.loads(revisions[1][1])["cell"]["qfq"]["close_price"] == 10.5
        assert json.loads(revisions[1][1])["industry"] == "bank"
        active = connection.execute(
            "SELECT payload_json FROM daily_records WHERE code='600001' AND trade_date='2026-09-02' "
            "ORDER BY first_seen_sequence DESC LIMIT 1"
        ).fetchone()
        active_payload = json.loads(active[0])
        assert active_payload["cell"]["unadjusted"]["close_price"] == 12.0
        assert active_payload["cell"]["qfq"]["close_price"] == 11.0
        assert active_payload["is_st"] is True
        assert active_payload["industry"] == "finance"

    repository = SQLiteHistoryControlRepository(target / "control.sqlite3")
    assert repository.integrity().state == "healthy"
    state = repository.load_state()
    assert state.active_snapshot is not None
    calendar = next(item for item in state.calendars if item.content_hash == state.active_snapshot.calendar_hash)
    assert tuple(item.isoformat() for item in calendar.open_dates) == ("2026-09-01", "2026-09-02")
    assert state.active_snapshot.content_hash == summary.snapshot_hash
    assert state.active_snapshot.sequence == 1
    assert state.active_snapshot.partitions[0].relative_path == "partitions/2026/09.sqlite3"
    for partition in state.active_snapshot.partitions:
        SQLiteHistoryMonthPartitionRepository.verify(target / partition.relative_path, partition)
    assert state.checkpoints[-1].state == "completed"
    assert state.due_states[-1].reason == "data_incomplete"


def test_converter_is_idempotent_and_does_not_rewrite_completed_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    first = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )
    before = _file_hashes(target)

    second = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )

    assert first.snapshot_hash == second.snapshot_hash
    assert second.state == "already_current"
    assert _file_hashes(target) == before


def test_converter_renames_completed_hash_layout_and_rebuilds_current_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )
    stable, hashed = _rewrite_completed_target_as_hash_layout(target)
    source_before = _file_hashes(source)
    with pytest.raises(HistoryControlError, match="payload is invalid"):
        SQLiteHistoryControlRepository(target / "control.sqlite3").load_state()

    summary = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )

    assert summary.state == "completed"
    assert stable.is_file()
    assert not hashed.exists()
    assert not hashed.parent.exists()
    assert _file_hashes(source) == source_before
    state = SQLiteHistoryControlRepository(target / "control.sqlite3").load_state()
    assert state.active_snapshot is not None
    assert state.active_snapshot.content_hash == summary.snapshot_hash
    assert state.active_snapshot.partitions[0].relative_path == "partitions/2026/09.sqlite3"
    archive = SQLiteHistoryTrainingInputArchive.open(target)
    assert archive.snapshot.training_codes == ("600001",)

    before_replay = _file_hashes(target)
    replay = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )
    assert replay.state == "already_current"
    assert replay.snapshot_hash == summary.snapshot_hash
    assert _file_hashes(target) == before_replay


def test_converter_resumes_hash_layout_rename_after_control_publication_is_interrupted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )
    stable, hashed = _rewrite_completed_target_as_hash_layout(target)

    def interrupt(stage: str) -> None:
        if stage == "hash_layout_partitions_renamed":
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        converter.convert_archive(
            source,
            target,
            batch_size=2,
            cache_mib=4,
            throttle_seconds=0,
            minimum_free_bytes=0,
            apply_niceness=False,
            fault_injector=interrupt,
        )

    assert stable.is_file()
    assert not hashed.exists()
    with pytest.raises(HistoryControlError, match="payload is invalid"):
        SQLiteHistoryControlRepository(target / "control.sqlite3").load_state()

    summary = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )
    assert summary.state == "completed"
    assert SQLiteHistoryControlRepository(target / "control.sqlite3").load_state().active_snapshot is not None


def test_converter_rejects_changed_hash_layout_partition_without_renaming_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
    )
    stable, hashed = _rewrite_completed_target_as_hash_layout(target)
    with hashed.open("ab") as handle:
        handle.write(b"changed")

    with pytest.raises(converter.ConversionError, match="hash-layout partition verification failed"):
        converter.convert_archive(
            source,
            target,
            batch_size=2,
            cache_mib=4,
            throttle_seconds=0,
            minimum_free_bytes=0,
            apply_niceness=False,
        )

    assert hashed.is_file()
    assert not stable.exists()


def test_converter_downloads_only_supplier_provable_missing_daily_fields(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    _remove_increment_qfq(source)

    seen = []

    def supplement(requests, **_kwargs):
        seen.extend(requests)
        payload = _json(_side("600001", "2026-09-02", "qfq", 11.25))
        return BaoStockGapResult(
            (BaoStockGapRecord("600001", date(2026, 9, 2), "daily_qfq", payload),),
            (),
        )

    summary = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
        supplement_missing=True,
        supplement_provider=supplement,
    )

    assert [(item.code, item.family, item.trade_dates) for item in seen] == [
        ("600001", "daily_qfq", (date(2026, 9, 2),))
    ]
    assert summary.supplemented_rows == 1
    assert summary.remaining_downloadable_gaps == 0
    partition = target / "partitions/2026/09.sqlite3"
    with sqlite3.connect(partition) as connection:
        row = connection.execute(
            "SELECT payload_json FROM daily_records WHERE code='600001' AND trade_date='2026-09-02' "
            "ORDER BY first_seen_sequence DESC LIMIT 1"
        ).fetchone()
    assert json.loads(row[0])["cell"]["qfq"]["close_price"] == 11.25


def test_converter_keeps_completed_months_resumable_when_gap_download_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    _remove_increment_qfq(source)

    def unavailable(_requests, **_kwargs):
        raise converter.BaoStockGapSupplierError("supplier_login_timeout")

    with pytest.raises(converter.ConversionError, match="supplementation failed"):
        converter.convert_archive(
            source,
            target,
            batch_size=2,
            cache_mib=4,
            throttle_seconds=0,
            minimum_free_bytes=0,
            apply_niceness=False,
            supplement_missing=True,
            supplement_provider=unavailable,
        )

    staging = target.with_name(".target-conversion")
    assert not target.exists()
    assert not (staging / "partitions/2026/08").exists()
    assert (staging / "partitions/2026/09.sqlite3").is_file()

    summary = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
        supplement_missing=False,
    )

    assert summary.state == "completed"
    assert summary.remaining_downloadable_gaps == 1
    assert not staging.exists()

    replay = converter.convert_archive(
        source,
        target,
        batch_size=2,
        cache_mib=4,
        throttle_seconds=0,
        minimum_free_bytes=0,
        apply_niceness=False,
        supplement_missing=False,
    )
    assert replay.state == "already_current"
    assert replay.active_rows == summary.active_rows
    assert replay.remaining_downloadable_gaps == 1


def test_converter_rejects_an_unrelated_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    target.mkdir()
    (target / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(converter.ConversionError, match="target already exists"):
        converter.convert_archive(
            source,
            target,
            minimum_free_bytes=0,
            apply_niceness=False,
        )

    assert (target / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_converter_rejects_a_broken_source_manifest_identity_chain(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)
    active_path = source / "active-manifest.json"
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active["parent_manifest_file_hash"] = "f" * 64
    active_path.write_text(_json(active), encoding="utf-8")

    with pytest.raises(converter.ConversionError, match="identity chain"):
        converter.convert_archive(
            source,
            target,
            minimum_free_bytes=0,
            apply_niceness=False,
        )

    assert not target.exists()


def test_converter_rejects_and_preserves_an_unrelated_staging_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    staging = tmp_path / ".target-conversion"
    _create_source(source)
    staging.mkdir()
    (staging / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(converter.ConversionError, match="not a resumable conversion"):
        converter.convert_archive(
            source,
            target,
            minimum_free_bytes=0,
            apply_niceness=False,
        )

    assert (staging / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_resource_defaults_are_single_process_and_bounded() -> None:
    parser = converter.build_parser()
    args = parser.parse_args([])

    assert args.batch_size <= 512
    assert args.cache_mib <= 16
    assert args.throttle_ms >= 1
    assert args.minimum_free_mib >= 1024
    source = Path(converter.__file__).read_text(encoding="utf-8")
    assert "multiprocessing" not in source
    assert "ThreadPoolExecutor" not in source
    assert "PRAGMA temp_store=FILE" in source
    assert "qualification" not in converter.DOWNLOADABLE_FIELD_FAMILIES
    assert "industry" not in converter.DOWNLOADABLE_FIELD_FAMILIES


def test_cli_help_documents_atomic_resume_and_resource_controls(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        converter.main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    for token in (
        "--batch-size",
        "--cache-mib",
        "--throttle-ms",
        "--minimum-free-mib",
        "--offline",
        "可续传",
        "旁路",
    ):
        assert token in output


def test_cli_displays_bounded_validation_and_conversion_progress(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    _create_source(source)

    assert (
        converter.main(
            [
                "--source",
                str(source),
                "--target",
                str(target),
                "--batch-size",
                "2",
                "--cache-mib",
                "4",
                "--throttle-ms",
                "0",
                "--minimum-free-mib",
                "0",
                "--no-nice",
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert "阶段=源校验" in captured.err
    assert "阶段=转换" in captured.err
    assert "阶段=缺口扫描" in captured.err
    assert "阶段=补缺下载" in captured.err
    assert "月份=2026-09" in captured.err
    assert "进度=100.00%" in captured.err
    assert "已处理=6/6" in captured.err
    assert '"state": "completed"' in captured.out


def test_sqlite_runtime_is_forced_to_one_thread_without_memory_mapping() -> None:
    source = Path(converter.__file__).read_text(encoding="utf-8")

    assert "PRAGMA threads=1" in source
    assert "PRAGMA mmap_size=0" in source
