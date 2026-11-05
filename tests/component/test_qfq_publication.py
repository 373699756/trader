from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

import math
import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.qfq import QFQ_CONFIGURATION
from trader.download.application.update_qfq import QfqPairPublicationError
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction

_DAYS = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(4))


def _window(code: str, *, price: float = 10.0) -> PublishedHistoryWindow:
    cells = []
    for day in _DAYS:
        raw = PublishedHistorySide(
            code,
            day,
            "unadjusted",
            price,
            price,
            price,
            price,
            100.0,
            1000.0,
            None,
            None,
            None,
            "trading",
        )
        cells.append(PublishedHistoryCell(code, day, "complete", raw, replace(raw, adjustment="qfq")))
    return PublishedHistoryWindow(code, tuple(cells))


def test_batched_reads_scan_publication_contents_once(tmp_path, monkeypatch):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = tuple(_window(f"600{number:03d}") for number in range(65))
    primary.replace_paired_windows(
        peer, windows, "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    )
    scans = []
    original = SqliteConnection.run

    def observed(connection, sql, parameters=()):
        if sql.startswith("SELECT code,content_hash,source,start_date,end_date FROM window_binding"):
            scans.append(sql)
        return original(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", observed)
    manifest = primary.manifest()
    assert manifest is not None
    assert tuple(primary.iter_windows(manifest, sessions=len(_DAYS))) == windows
    assert len(scans) == 1
    assert SQLiteQfqWindowCache(primary.path, "v2", sqlite_policy=SqlitePolicyFixture()).manifest() == manifest


@pytest.mark.parametrize("selected", (frozenset(), frozenset({"600002", "600999"})))
def test_selected_window_stream_reads_only_requested_prices_and_preserves_publication(tmp_path, monkeypatch, selected):
    import trader.download.infra.qfq_sqlite as module

    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = tuple(_window(f"600{number:03d}") for number in range(3))
    primary.replace_paired_windows(peer, windows, "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1])
    manifest = primary.manifest()
    read_batches, decoded = [], []
    read = primary._batch_rows
    decode = module.PriceCellFromColumns

    def read_selected(connection, codes):
        read_batches.append(tuple(codes))
        assert set(codes) <= selected
        return read(connection, codes)

    def decode_selected(code, *args, **kwargs):
        decoded.append(code)
        assert code in selected
        return decode(code, *args, **kwargs)

    monkeypatch.setattr(primary, "_batch_rows", read_selected)
    monkeypatch.setattr(module, "PriceCellFromColumns", decode_selected)
    assert tuple(primary.iter_windows(manifest, sessions=len(_DAYS), codes=selected)) == tuple(w for w in windows if w.code in selected)
    assert read_batches == ([("600002",)] if selected else [])
    assert len(decoded) == (len(_DAYS) if selected else 0)
    assert primary.manifest() == manifest


def test_empty_window_selection_still_checks_cancellation_and_original_publication(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    primary.replace_paired_windows(peer, (_window("600001"),), "old", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1])
    manifest = primary.manifest()

    def cancelled():
        raise RuntimeError("history_preload_cancelled")

    with pytest.raises(RuntimeError, match="history_preload_cancelled"):
        tuple(primary.iter_windows(manifest, sessions=len(_DAYS), codes=frozenset(), check=cancelled))
    primary.replace_paired_windows(peer, (_window("600001", price=11.0),), "new", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1])
    with pytest.raises(RuntimeError, match="qfq_publication_changed"):
        tuple(primary.iter_windows(manifest, sessions=len(_DAYS), codes=frozenset()))


def test_publication_failure_rolls_back_prices_both_profiles_and_checkpoint(tmp_path, monkeypatch):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    primary.replace_paired_windows(
        peer, (_window("600001"),), "old", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    )
    manifests = (primary.manifest(), peer.manifest())
    original = SqliteConnection.run

    def interrupted(connection, sql, parameters=()):
        if sql.startswith("INSERT INTO window_publication") and parameters[0] == "v3":
            raise sqlite3.OperationalError("fixture publication interrupted")
        return original(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", interrupted)
    with pytest.raises(QfqPairPublicationError):
        primary.replace_paired_windows(
            peer,
            (_window("600001", price=12.0),),
            "new",
            QFQ_CONFIGURATION,
            calendar=_DAYS,
            completed_day=_DAYS[-1],
        )
    assert (primary.manifest(), peer.manifest()) == manifests
    assert primary.read_code("600001") == peer.read_code("600001") == _window("600001")
    assert primary.completed(_DAYS[-1], "600001", "old")
    assert not primary.completed(_DAYS[-1], "600001", "new")


def test_numeric_types_and_negative_zero_keep_window_identity_after_restart(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    original = _window("600001")
    first = original.cells[0]
    raw = replace(first.unadjusted, open_price=10, volume=100, pct_change=-0.0)
    window = replace(original, cells=(replace(first, unadjusted=raw), *original.cells[1:]))
    primary.replace_paired_windows(
        peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    )
    manifest = primary.manifest()
    fresh = SQLiteQfqWindowCache(primary.path, "v2", sqlite_policy=SqlitePolicyFixture())
    assert fresh.manifest() == manifest
    decoded = fresh.read_code(window.code).cells[0].unadjusted
    assert type(decoded.open_price) is int
    assert math.copysign(1, decoded.pct_change) == -1
    fresh.replace_paired_windows(
        peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    )
    assert fresh.manifest() == manifest
    with SqliteSession(primary.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price SET number_flags=0")
    with pytest.raises(RuntimeError, match="checksum_mismatch"):
        fresh.read_code(window.code)


@pytest.mark.parametrize("change", ("source", "calendar", "remove"))
def test_old_publication_is_rejected_after_each_identity_change(tmp_path, change):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = (_window("600001"), _window("600002"))
    primary.replace_paired_windows(
        peer, windows, "old", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    )
    manifest = primary.manifest()
    assert manifest is not None
    if change == "remove":
        primary.select_published_codes(peer, frozenset({"600002"}))
    else:
        calendar = _DAYS if change == "source" else (*_DAYS, _DAYS[-1] + timedelta(days=1))
        primary.replace_paired_windows(
            peer,
            windows,
            "new" if change == "source" else "old",
            QFQ_CONFIGURATION,
            calendar=calendar,
            completed_day=calendar[-1],
        )
    with pytest.raises(RuntimeError, match="qfq_publication_changed"):
        primary.read_windows(manifest, ("600001",), sessions=len(manifest.calendar_dates))
    current = primary.manifest()
    assert current is not None and current != manifest
    assert tuple(primary.iter_windows(current, sessions=len(current.calendar_dates))) == (
        windows[1:] if change == "remove" else windows
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE window_binding SET content_hash=zeroblob(32) WHERE profile='v2'",
        "DELETE FROM window_calendar WHERE profile='v2'",
        "UPDATE window_publication SET publication_id='corrupt' WHERE profile='v2'",
        "DELETE FROM window_publication WHERE profile='v2'",
    ),
)
def test_cold_manifest_rejects_unpublished_or_corrupt_metadata(tmp_path, mutation):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    primary.replace_paired_windows(
        peer,
        (_window("600001"),),
        "fixture",
        QFQ_CONFIGURATION,
        calendar=_DAYS,
        completed_day=_DAYS[-1],
    )
    with SqliteSession(primary.path) as connection, SqliteTransaction(connection):
        connection.run(mutation)
    with pytest.raises(RuntimeError):
        fresh = SQLiteQfqWindowCache(primary.path, "v2", sqlite_policy=SqlitePolicyFixture())
        manifest = fresh.manifest()
        assert manifest is not None
        tuple(fresh.iter_windows(manifest, sessions=len(manifest.calendar_dates)))


def test_empty_population_preserves_paired_facts_and_completion(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    window = _window("600001")
    primary.replace_paired_windows(
        peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    )
    primary.select_published_codes(peer, frozenset())
    assert primary.manifest() is None
    assert peer.manifest() is None
    assert peer.read_code(window.code) == window
    assert primary.read_code(window.code) == window
    assert primary.completed(_DAYS[-1], window.code, "fixture")
    assert primary.codes() == peer.codes() == frozenset()
    assert primary.select_published_codes(peer, frozenset({window.code})) == ((primary.path.name,), 0)
    assert primary.codes() == peer.codes() == frozenset({window.code})


def test_obsolete_schema_is_rejected_without_rewriting_it(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    primary.replace_paired_windows(
        peer,
        (_window("600001"),),
        "fixture",
        QFQ_CONFIGURATION,
        calendar=_DAYS,
        completed_day=_DAYS[-1],
    )
    with SqliteSession(primary.path) as connection, SqliteTransaction(connection):
        connection.run("DROP TABLE window_publication")
        connection.run("PRAGMA user_version=1")
    before = primary.path.read_bytes()
    with pytest.raises(RuntimeError, match="sqlite_publication_schema_invalid"):
        primary.manifest()
    with pytest.raises(QfqPairPublicationError):
        primary.replace_paired_windows(
            peer,
            (_window("600001"),),
            "fixture",
            QFQ_CONFIGURATION,
            calendar=_DAYS,
            completed_day=_DAYS[-1],
        )
    assert primary.path.read_bytes() == before


def test_unchanged_pair_uses_committed_identity_without_reading_prices(tmp_path, monkeypatch):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = (_window("600001"),)
    primary.replace_paired_windows(peer, windows, "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1])
    original = SqliteConnection.run

    def observed(connection, sql, parameters=()):
        assert "FROM daily_price" not in sql
        return original(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", observed)
    assert primary.replace_paired_windows(
        peer, windows, "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1]
    ) == ((), 0)


def test_population_change_is_atomic_and_keeps_prices_bindings_and_receipts(tmp_path, monkeypatch):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = (_window("600001"), _window("600002"))
    primary.replace_paired_windows(peer, windows, "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1])
    manifests = primary.manifest(), peer.manifest()
    with SqliteSession(primary.path) as connection:
        before = tuple(
            tuple(tuple(row) for row in connection.run(f"SELECT * FROM {table} ORDER BY 1,2"))
            for table in ("daily_price", "window_binding", "window_completion")
        )
    run = SqliteConnection.run

    def interrupted(connection, sql, parameters=()):
        if sql == "DELETE FROM window_population WHERE profile=?" and parameters == ("v3",):
            raise sqlite3.OperationalError("fixture_population_interrupted")
        return run(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", interrupted)
    with pytest.raises(sqlite3.OperationalError, match="fixture_population_interrupted"):
        primary.select_published_codes(peer, frozenset({"600001"}))
    monkeypatch.setattr(SqliteConnection, "run", run)
    assert (primary.manifest(), peer.manifest()) == manifests
    assert primary.select_published_codes(peer, frozenset({"600001"})) == ((primary.path.name,), 0)
    assert primary.codes() == peer.codes() == frozenset({"600001"})
    with pytest.raises(RuntimeError, match="qfq_publication_changed"):
        primary.read_windows(manifests[0], ("600002",), sessions=len(_DAYS))
    current = primary.manifest()
    assert primary.read_windows(current, ("600001", "600002"), sessions=len(_DAYS)) == windows[:1]
    with SqliteSession(primary.path) as connection:
        assert tuple(
            tuple(tuple(row) for row in connection.run(f"SELECT * FROM {table} ORDER BY 1,2"))
            for table in ("daily_price", "window_binding", "window_completion")
        ) == before


def test_only_current_eligibility_writer_can_add_a_missing_population_declaration(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2", sqlite_policy=SqlitePolicyFixture())
    peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture())
    windows = (_window("600001"), _window("600002"))
    primary.replace_paired_windows(peer, windows, "fixture", QFQ_CONFIGURATION, calendar=_DAYS, completed_day=_DAYS[-1])
    with SqliteSession(primary.path) as connection, SqliteTransaction(connection):
        connection.run("DROP TABLE window_population")
    primary.publish_file()
    before = primary.path.read_bytes()
    with pytest.raises(RuntimeError, match="qfq_manifest_unavailable"):
        primary.manifest()
    assert primary.path.read_bytes() == before
    primary.select_published_codes(peer, frozenset({"600001"}))
    assert primary.manifest().universe_codes == peer.manifest().universe_codes == ("600001",)
    assert primary.completed(_DAYS[-1], "600002", "fixture")
    assert primary.read_code("600002") == windows[1]
    with SqliteSession(primary.path) as connection:
        assert connection.run("PRAGMA user_version").require_one()[0] == 3
