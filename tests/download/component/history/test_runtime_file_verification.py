"""Normal download publication seeds the same cache used by runtime readers."""

from dataclasses import replace
from datetime import date

import pytest

from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.download.history.infra.history_sync import FakeSupplier, NOW
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.download.qfq import QFQ_CONFIGURATION, qfq_window
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_verification_cache import SqliteVerificationCache
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.domain.download_failure import DownloadFailureContext


def test_history_download_publication_seeds_startup_cache_for_each_year(tmp_path, monkeypatch):
    root = tmp_path / "history"
    directory = tmp_path / "runtime" / "sqlite-verification"
    configuration = HistorySyncConfigurationFixture(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=tmp_path / "reference",
    )
    result = RunHistoryFixture(
        configuration, FakeSupplier((date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2))),
        clock=lambda: NOW, verification_cache=SqliteVerificationCache(directory),
    )
    assert result.state == "completed"

    def forbidden(*_args):
        pytest.fail("startup must reuse the annual file hash saved by normal synchronization")

    monkeypatch.setattr("trader.infra.sqlite_publication._digest", forbidden)
    restarted = SQLitePublishedHistorySnapshot(root, sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))
    manifest = restarted.manifest()
    assert manifest is not None
    windows = tuple(restarted.iter_windows(manifest, sessions=3))
    assert len(windows) == 2 and all(len(window.cells) == 3 for window in windows)


def test_market_update_seeds_restart_cache_without_rehashing_rows_or_database(tmp_path, monkeypatch):
    path = tmp_path / "daily_window.sqlite3"
    directory = tmp_path / "runtime" / "sqlite-verification"
    cache = SqliteVerificationCache(directory)
    primary = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)
    peer = SQLiteQfqWindowCache(path, "v3", sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)
    window = qfq_window("600001")
    window = replace(window, cells=window.cells[-251:])
    dates = tuple(cell.trade_date for cell in window.cells)
    primary.replace_paired_windows(peer, (window,), "fixture", QFQ_CONFIGURATION, calendar=dates, completed_day=dates[-1])

    def forbidden(*_args):
        pytest.fail("unchanged market reads must not rehash stored rows or file bodies")

    monkeypatch.setattr("trader.infra.sqlite_publication._digest", forbidden)
    monkeypatch.setattr("trader.download.infra.qfq_sqlite._digest", forbidden)
    restarted = SQLiteQfqWindowCache(path, "v2", sqlite_policy=SqlitePolicyFixture(), verification_cache=SqliteVerificationCache(directory))
    assert restarted.manifest() is not None
    assert restarted.read_code("600001").cells == window.cells


@pytest.mark.parametrize("target", ("history", "market"))
def test_normal_sync_automatically_injects_cache_and_batches_failure_publication(tmp_path, monkeypatch, target):
    from trader import bootstrap
    from trader.infra.settings.data_paths import DataPaths
    from tests.helpers.shared.configuration import RuntimeSettingsFixture
    from tests.helpers.shared.reference_fixtures import PreparedFixture
    from trader.infra import sqlite_publication

    settings = replace(RuntimeSettingsFixture(), runtime_dir=tmp_path / "runtime", data_paths=DataPaths(tmp_path / "data"))
    monkeypatch.setattr(bootstrap, "LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(bootstrap, "BuildSqlitePolicy", lambda _: SqlitePolicyFixture())
    monkeypatch.setattr(bootstrap, "_prepare_reference", lambda *_, **__: PreparedFixture(NOW))
    original = sqlite_publication._digest
    hashed = []

    def digest(path, size, check):
        hashed.append(path)
        return original(path, size, check)

    monkeypatch.setattr(sqlite_publication, "_digest", digest)

    def history(*_args, **options):
        assert options["verification_cache"].directory == settings.runtime_dir / "sqlite-verification"
        return HistoryMaintenanceStatus("completed", None, settings.data_paths.history, "tencent", None,
                                        "a" * 64, NOW.date(), NOW.date(), 0)

    def market(*_args, **options):
        primary = options["primary"]
        cache = options["verification_cache"]
        assert cache.directory == settings.runtime_dir / "sqlite-verification"
        peer = SQLiteQfqWindowCache(primary.path, "v3", sqlite_policy=SqlitePolicyFixture(), verification_cache=cache)
        with primary.publication(peer):
            for _ in range(3):
                options["failures"](DownloadFailureContext("market_command"), RuntimeError("source_unavailable"))
        assert hashed == [settings.data_paths.daily_window]
        return QfqUpdateResult(NOW.date(), 1)

    monkeypatch.setattr(bootstrap, "ExecuteHistoryDownload", history)
    monkeypatch.setattr(bootstrap, "ExecuteQfqDownload", market)
    assert bootstrap.ExecuteSync(settings.config_path, target).exit_code == 0
