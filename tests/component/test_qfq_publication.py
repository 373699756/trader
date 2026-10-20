from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest

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
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2")
    peer = SQLiteQfqWindowCache(primary.path, "v3")
    windows = tuple(_window(f"600{number:03d}") for number in range(65))
    primary.replace_paired_windows(peer, windows, "fixture", calendar=_DAYS, completed_day=_DAYS[-1])
    scans = []
    original = SqliteConnection.run

    def observed(connection, sql, parameters=()):
        if sql.startswith("SELECT code,digest,source,first_day,cutoff FROM identities"):
            scans.append(sql)
        return original(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", observed)
    manifest = primary.manifest()
    assert manifest is not None
    assert tuple(primary.iter_windows(manifest, sessions=251)) == windows
    assert len(scans) == 1
    assert SQLiteQfqWindowCache(primary.path, "v2").manifest() == manifest


def test_publication_failure_rolls_back_prices_both_profiles_and_checkpoint(tmp_path, monkeypatch):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2")
    peer = SQLiteQfqWindowCache(primary.path, "v3")
    primary.replace_paired_windows(peer, (_window("600001"),), "old", calendar=_DAYS, completed_day=_DAYS[-1])
    manifests = (primary.manifest(), peer.manifest())
    original = SqliteConnection.run

    def interrupted(connection, sql, parameters=()):
        if sql.startswith("INSERT INTO publications") and parameters[0] == "v3":
            raise sqlite3.OperationalError("fixture publication interrupted")
        return original(connection, sql, parameters)

    monkeypatch.setattr(SqliteConnection, "run", interrupted)
    with pytest.raises(QfqPairPublicationError):
        primary.replace_paired_windows(
            peer, (_window("600001", price=12.0),), "new", calendar=_DAYS, completed_day=_DAYS[-1]
        )
    assert (primary.manifest(), peer.manifest()) == manifests
    assert primary.read_code("600001") == peer.read_code("600001") == _window("600001")
    assert primary.completed(_DAYS[-1], "600001", "old")
    assert not primary.completed(_DAYS[-1], "600001", "new")


@pytest.mark.parametrize("change", ("source", "calendar", "remove"))
def test_old_publication_is_rejected_after_each_identity_change(tmp_path, change):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2")
    peer = SQLiteQfqWindowCache(primary.path, "v3")
    windows = (_window("600001"), _window("600002"))
    primary.replace_paired_windows(peer, windows, "old", calendar=_DAYS, completed_day=_DAYS[-1])
    manifest = primary.manifest()
    assert manifest is not None
    if change == "remove":
        primary.retain_codes(frozenset({"600002"}))
    else:
        calendar = _DAYS if change == "source" else (*_DAYS, _DAYS[-1] + timedelta(days=1))
        primary.replace_paired_windows(
            peer,
            windows,
            "new" if change == "source" else "old",
            calendar=calendar,
            completed_day=calendar[-1],
        )
    with pytest.raises(RuntimeError, match="qfq_publication_changed"):
        primary.read_windows(manifest, ("600001",), sessions=251)
    current = primary.manifest()
    assert current is not None and current != manifest
    assert tuple(primary.iter_windows(current, sessions=251)) == (windows[1:] if change == "remove" else windows)


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE identities SET digest='corrupt' WHERE profile='v2'",
        "DELETE FROM calendars WHERE profile='v2'",
        "UPDATE publications SET identity='corrupt' WHERE profile='v2'",
        "DELETE FROM publications WHERE profile='v2'",
    ),
)
def test_cold_manifest_rejects_unpublished_or_corrupt_metadata(tmp_path, mutation):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2")
    peer = SQLiteQfqWindowCache(primary.path, "v3")
    primary.replace_paired_windows(peer, (_window("600001"),), "fixture", calendar=_DAYS, completed_day=_DAYS[-1])
    with SqliteSession(primary.path) as connection, SqliteTransaction(connection):
        connection.run(mutation)
    with pytest.raises(RuntimeError):
        fresh = SQLiteQfqWindowCache(primary.path, "v2")
        manifest = fresh.manifest()
        assert manifest is not None
        tuple(fresh.iter_windows(manifest, sessions=251))


def test_removing_last_code_clears_only_its_profile_publication(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2")
    peer = SQLiteQfqWindowCache(primary.path, "v3")
    window = _window("600001")
    primary.replace_paired_windows(peer, (window,), "fixture", calendar=_DAYS, completed_day=_DAYS[-1])
    peer_manifest = peer.manifest()
    primary.retain_codes(frozenset())
    assert primary.manifest() is None
    assert peer.manifest() == peer_manifest
    assert peer.read_code(window.code) == window
    peer.retain_codes(frozenset())
    assert peer.manifest() is None
    assert not primary.read_code(window.code).cells


def test_obsolete_schema_is_rejected_without_rewriting_it(tmp_path):
    primary = SQLiteQfqWindowCache(tmp_path / "daily_window.sqlite3", "v2")
    peer = SQLiteQfqWindowCache(primary.path, "v3")
    primary.replace_paired_windows(peer, (_window("600001"),), "fixture", calendar=_DAYS, completed_day=_DAYS[-1])
    with SqliteSession(primary.path) as connection, SqliteTransaction(connection):
        connection.run("DROP TABLE publications")
        connection.run("PRAGMA user_version=1")
    before = primary.path.read_bytes()
    with pytest.raises(RuntimeError, match="qfq_manifest_unavailable"):
        primary.manifest()
    with pytest.raises(QfqPairPublicationError):
        primary.replace_paired_windows(peer, (_window("600001"),), "fixture", calendar=_DAYS, completed_day=_DAYS[-1])
    assert primary.path.read_bytes() == before
