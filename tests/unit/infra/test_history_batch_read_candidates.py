"""An explicit batch-read candidate preserves exact keys and revision visibility."""

from dataclasses import replace
from datetime import date

import pytest

from tests.helpers.configuration import SqlitePolicyFixture
from tests.helpers.history import RunHistoryFixture
from tests.unit.infra.research.test_history_sync import NOW, FakeSupplier, _configuration, _download
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.history_direct_prices import HistoryDirectPrices, HistoryPriceResult
from trader.download.infra.history_download_index import HistoryDownloadTask
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity


def _prices(root, sequence=1, *, fresh=False, cancel=lambda: False):
    return HistoryDirectPrices(
        root,
        sequence,
        None,
        HistorySqliteActivity(lambda: None, cancel),
        fresh=fresh,
        sqlite_policy=SqlitePolicyFixture(),
    )


def test_batch_reads_each_year_once_and_matches_per_stock_sequence_visibility(tmp_path, monkeypatch):
    days = (date(2025, 1, 2), date(2025, 12, 4), date(2026, 1, 5))
    tasks = tuple(HistoryDownloadTask(code, days) for code in ("600001", "600002", "600003"))
    for year in (2025, 2026):
        partition = HistoryPricePartition(tmp_path / f"prices_{year}.sqlite3", year, 1, sqlite_policy=SqlitePolicyFixture())
        partition.initialize()
        partition.save_revisions(tuple(sorted((
            HistoryRevision(2 if cell.trade_date == days[-1] else 1, "main", cell, None, None, None)
            for task in tasks for cell in _download(task.code, days).batch.cells if cell.trade_date.year == year
        ), key=lambda revision: (revision.trade_date, revision.code))))
    prices = _prices(tmp_path)
    expected = tuple(prices.read_for_download(task.code, task.dates) for task in tasks)
    opened = []
    original = HistoryPricePartition._read_connection

    def counted(partition):
        opened.append(partition._path.name)
        return original(partition)

    monkeypatch.setattr(HistoryPricePartition, "_read_connection", counted)
    values = prices.read_for_download_batch(tasks)

    assert tuple(value.code for value in values) == tuple(task.code for task in tasks)
    assert tuple(value.cells for value in values) == expected
    assert all(tuple(cell.trade_date for cell in value.cells) == days[:-1] for value in values)
    assert opened == ["prices_2025.sqlite3", "prices_2026.sqlite3"]


def test_fresh_wave_skips_old_files_but_reads_its_own_durable_retry(tmp_path):
    days = (date(2026, 9, 8), date(2026, 9, 9))
    task = HistoryDownloadTask("600001", days)
    prices = _prices(tmp_path, fresh=True)
    assert prices.read_for_download_batch((task,))[0].cells == ()
    cells = _download(task.code, days).batch.cells
    prices.commit((HistoryPriceResult(task, tuple(HistoryRevision(1, "main", cell, None, None, None) for cell in cells), (), None),))
    assert prices.read_for_download_batch((task,))[0].cells == cells
    assert prices.read_for_download_batch((replace(task, code="600002"),))[0].cells == ()


def test_batch_rejects_duplicate_codes_and_capacity_before_reading(tmp_path):
    prices = _prices(tmp_path)
    task = HistoryDownloadTask("600001", (date(2026, 9, 8),))
    with pytest.raises(ValueError, match="history_download_read_duplicate_code"):
        prices.read_for_download_batch((task, task))
    with pytest.raises(ValueError, match="history_download_read_capacity_exceeded"):
        prices.read_for_download_batch(tuple(replace(task, code=f"{index:06d}") for index in range(65)))


def test_batch_read_propagates_user_cancellation(tmp_path):
    day = date(2026, 9, 8)
    partition = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    partition.initialize()
    with pytest.raises(KeyboardInterrupt):
        _prices(tmp_path, cancel=lambda: True).read_for_download_batch((HistoryDownloadTask("600001", (day,)),))


def test_explicit_candidate_finishes_a_real_isolated_incremental_pipeline(tmp_path):
    first = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    second = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    configuration = _configuration(tmp_path)
    assert RunHistoryFixture(configuration, FakeSupplier(first), clock=lambda: NOW).state == "completed"
    status = RunHistoryFixture(configuration, FakeSupplier(second), clock=lambda: NOW, batched_price_reads=True)
    assert status.state == "completed", status.reason
    prices = _prices(tmp_path, sequence=2)
    for code in ("600001", "600002"):
        assert prices.read(code, second) == _download(code, second).batch.cells
    assert status.unresolved_price_cells == 0
