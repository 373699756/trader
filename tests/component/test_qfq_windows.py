from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.domain.qfq_window import QfqUpdateResult, completed_daily_cutoff
from trader.download.infra.history_control_repository import HistoryMaintenanceLock
from trader.download.infra.qfq_checkpoint import QfqCheckpoint
from trader.download.infra.qfq_maintenance import QfqDailyMaintenance
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.qfq_update_runner import QfqUpdateRunner
from trader.entrypoints import cli
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

SHANGHAI = ZoneInfo("Asia/Shanghai")
DAYS = tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(252))


def _cell(code: str, day: date, *, price: float = 10.0) -> BaoStockDailyCell:
    raw = BaoStockDailySide(
        code,
        day,
        "unadjusted",
        price,
        price + 0.2,
        price - 0.2,
        price,
        100000.0,
        10000000.0,
        price - 0.01,
        0.1,
        1.0,
        "trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return BaoStockDailyCell(code, day, "complete", raw, qfq)


def _window(code: str = "600001", days: tuple[date, ...] = DAYS, *, price: float = 10.0) -> PublishedHistoryWindow:
    return PublishedHistoryWindow(code, tuple(project_history_cell(_cell(code, day, price=price)) for day in days))


def _fingerprints(root: Path) -> dict[str, tuple[str, int]]:
    return {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in root.iterdir()
        if path.is_file()
    }


@pytest.mark.parametrize("profile,sessions", (("v2", 251), ("v3", 61)))
def test_sqlite_rolls_windows_and_noop_is_byte_identical(tmp_path, profile, sessions) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, profile)
    changed, count = cache.replace_window(_window(days=DAYS[:-1]), "history:seed")
    assert f"{profile}/index.json" in changed
    assert count == sessions
    before = _fingerprints(cache.root)
    assert cache.replace_window(_window(days=DAYS[:-1]), "baostock:new-provenance") == ((), 0)
    assert _fingerprints(cache.root) == before
    index_before = (cache.root / "index.json").read_bytes()
    changed, count = cache.replace_window(_window(), "baostock:refresh")
    assert count == 2
    assert len(changed) == 1 and changed[0].endswith(".sqlite3")
    assert (cache.root / "index.json").read_bytes() == index_before
    assert tuple(cell.trade_date for cell in cache.read_code("600001").cells) == DAYS[-sessions:]
    manifest = cache.manifest()
    assert manifest is not None
    assert cache.read_windows(manifest, ("600001",), sessions=sessions)[0] == cache.read_code("600001")
    assert max(path.stat().st_size for path in cache.root.glob("*.sqlite3")) < 10_000_000


def test_changed_code_does_not_rewrite_other_range(tmp_path) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    cache.replace_window(_window("600001"), "seed")
    cache.replace_window(_window("300001"), "seed")
    membership = json.loads((cache.root / "index.json").read_text())
    before = _fingerprints(cache.root)
    changed, count = cache.replace_window(_window("600001", price=11.0), "revision")
    assert changed == (f"v2/{membership['600001']}",)
    assert count == 251
    assert _fingerprints(cache.root)[membership["300001"]] == before[membership["300001"]]
    assert _fingerprints(cache.root)["index.json"] == before["index.json"]


def test_preemptive_split_keeps_stable_membership_and_hard_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("trader.download.infra.qfq_sqlite.QFQ_SPLIT_BYTES", 180_000)
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    for code in ("600001", "600002", "600003", "600004"):
        cache.replace_window(_window(code), "seed")
    membership = json.loads((cache.root / "index.json").read_text())
    assert len(set(membership.values())) > 1
    before = (cache.root / "index.json").read_bytes()
    cache.replace_window(_window("600001"), "same")
    assert (cache.root / "index.json").read_bytes() == before
    assert all(path.stat().st_size < 180_000 for path in cache.root.glob("*.sqlite3"))
    assert all(len(cache.read_code(code).cells) == 251 for code in membership)


def test_tampered_cells_fail_closed(tmp_path) -> None:
    cache = SQLiteQfqWindowCache(tmp_path, "v2")
    cache.replace_window(_window(), "seed")
    shard = next(cache.root.glob("*.sqlite3"))
    with sqlite3.connect(shard) as connection:
        connection.execute("DELETE FROM bars WHERE day=?", (DAYS[-1].isoformat(),))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        cache.read_code("600001")


def test_checkpoint_restart_preserves_all_codes_and_failed_write_does_not_advance(tmp_path, monkeypatch) -> None:
    path = tmp_path / ".checkpoint.json"
    checkpoint = QfqCheckpoint(path)
    for code in ("600001", "300001", "600002"):
        checkpoint.confirm(DAYS[-1], code)
    checkpoint = QfqCheckpoint(path)
    assert all(checkpoint.completed(DAYS[-1], code) for code in ("600001", "300001", "600002"))

    def fail(*_args):
        raise OSError("write failed")

    monkeypatch.setattr("trader.download.infra.qfq_checkpoint.atomic_write_json", fail)
    with pytest.raises(OSError):
        checkpoint.confirm(DAYS[-1], "600003")
    assert not checkpoint.completed(DAYS[-1], "600003")
    assert not checkpoint.completed(DAYS[-1] + timedelta(days=1), "600001")


@dataclass
class Supplier:
    days: tuple[date, ...] = DAYS[1:]
    codes: tuple[str, ...] = ("600001",)
    conflict: bool = False
    fail: bool = False
    calls: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        assert sessions == 251
        return HistorySupplierContext(
            BaoStockCalendar(self.days),
            tuple(BaoStockSecurity(code, "fixture", "main", DAYS[0], None, "fixture") for code in self.codes),
            BaoStockSourceVersions("fixture", "3.11", ()),
        )

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        self.calls.append((security.code, dates))
        if self.fail:
            raise RuntimeError("supplier fixture failure")
        cells = tuple(_cell(security.code, day, price=11.0 if self.conflict else 10.0) for day in dates)
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, cells),
            tuple(BaoStockDailyFact(security.code, day, False) for day in dates),
        )


def _updater(tmp_path: Path, supplier: Supplier) -> UpdateQfqWindows:
    return UpdateQfqWindows(
        SQLiteQfqWindowCache(tmp_path, "v2"),
        SQLiteQfqWindowCache(tmp_path, "v3"),
        supplier,
        QfqCheckpoint(tmp_path / ".checkpoint.json"),
        lambda: False,
        lambda _message: None,
    )


def test_one_serial_fetch_fills_both_profiles_and_restart_skips_completed(tmp_path) -> None:
    supplier = Supplier(codes=("600001", "300001"))
    updater = _updater(tmp_path, supplier)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.completed_codes == 2 and result.pending_codes == 0
    assert len(supplier.calls) == 2
    assert len(updater.v2.read_code("600001").cells) == 251
    assert len(updater.v3.read_code("600001").cells) == 61
    before = (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3"))
    result = _updater(tmp_path, supplier).execute(observed)
    assert result.skipped_codes == 2 and result.changed_files == ()
    assert len(supplier.calls) == 2
    assert (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3")) == before


@pytest.mark.parametrize("conflict", (False, True))
def test_small_tail_exact_overlap_or_bounded_revision_refetch(tmp_path, conflict) -> None:
    supplier = Supplier(conflict=conflict)
    updater = _updater(tmp_path, supplier)
    updater.seed((_window(days=DAYS[:-1]),), "history:seed")
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.pending_codes == 0
    assert supplier.calls[0][1] == DAYS[-4:]
    assert len(supplier.calls) == (2 if conflict else 1)
    if conflict:
        assert supplier.calls[1][1] == DAYS[-251:]
    assert tuple(cell.trade_date for cell in updater.v2.read_code("600001").cells) == DAYS[-251:]


def test_same_day_without_checkpoint_only_rechecks_three_overlap_dates(tmp_path) -> None:
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    updater.seed((_window(),), "history:seed")
    result = updater.execute(datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16))
    assert result.changed_files == ()
    assert supplier.calls == [("600001", DAYS[-3:])]


def test_interrupted_between_profiles_does_not_advance_checkpoint_and_retry_repairs(tmp_path, monkeypatch) -> None:
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    write = updater.v3.replace_window

    def fail(*_args):
        raise RuntimeError("interrupted second profile")

    monkeypatch.setattr(updater.v3, "replace_window", fail)
    observed = datetime.combine(DAYS[-1], datetime.min.time(), tzinfo=SHANGHAI).replace(hour=16)
    result = updater.execute(observed)
    assert result.pending_codes == 1
    assert not updater.resume.completed(DAYS[-1], "600001")
    assert updater.v2.read_code("600001").cells
    monkeypatch.setattr(updater.v3, "replace_window", write)
    result = updater.execute(observed)
    assert result.pending_codes == 0 and result.completed_codes == 1
    assert len(updater.v3.read_code("600001").cells) == 61
    assert updater.resume.completed(DAYS[-1], "600001")


@pytest.mark.parametrize(
    "hour,minute,due", ((8, 0, False), (12, 0, False), (15, 9, False), (15, 10, True), (21, 0, True))
)
def test_hot_and_late_cold_start_daily_schedule(hour, minute, due) -> None:
    now = datetime(2026, 10, 9, hour, minute, tzinfo=SHANGHAI)
    calls = []

    def update(cancel):
        assert not cancel()
        calls.append(now)
        return QfqUpdateResult(now.date(), 1)

    maintenance = QfqDailyMaintenance(update, now=lambda: now)
    assert maintenance.tick() is due
    assert len(calls) == int(due)
    assert not maintenance.tick()
    assert completed_daily_cutoff(now) == now.date() - timedelta(days=not due)


def test_retry_delay_completion_next_day_and_cold_missing_database() -> None:
    now = datetime(2026, 10, 9, 15, 10, tzinfo=SHANGHAI)
    elapsed = 0.0
    calls = 0

    def update(_cancel):
        nonlocal calls
        calls += 1
        return QfqUpdateResult(now.date(), pending_codes=int(calls == 1))

    maintenance = QfqDailyMaintenance(update, now=lambda: now, monotonic=lambda: elapsed)
    assert maintenance.tick()
    elapsed = 1799
    assert not maintenance.tick()
    elapsed = 1800
    assert maintenance.tick()
    elapsed = 4000
    assert not maintenance.tick()
    now += timedelta(days=1)
    assert maintenance.tick()
    now = now.replace(hour=8)
    cold = QfqDailyMaintenance(update, now=lambda: now, needs_initialization=lambda: True)
    assert cold.tick()
    result = cold.stop(wait=True)
    assert result.completed


def test_online_projection_matches_full_history_and_keeps_settlement_independent(tmp_path) -> None:
    from tests.component.test_candidate_history_tail import CALENDAR, NOW, _Archive

    full = _Archive(251, 0, ("600001", "600002"))
    reader = ReadPublishedHistoryUseCase(full)
    short = SQLiteQfqWindowCache(tmp_path, "v2")
    for window in reader.iter_windows(full.current, sessions=251):
        if window.code == "600002":
            window = PublishedHistoryWindow(window.code, window.cells[:-2])
        short.replace_window(window, "history:fixture")
    calendar_calls = 0

    def dates():
        nonlocal calendar_calls
        calendar_calls += 1
        return CALENDAR

    projection = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(short),
        lookback_sessions=251,
        outcome_history=reader,
        open_dates=dates,
    )
    original = PublishedHistoryCache(reader, lookback_sessions=251)
    assert projection.refresh() and original.refresh()
    restrictions = {}
    loaded = projection.load(("600001", "600002"), observed_at=NOW, action_restrictions=restrictions)
    assert tuple(loaded) == ("600001",)
    assert calendar_calls == 1
    assert restrictions == {"600002": {"history_data_pending"}}
    expected = original.load(("600001",), observed_at=NOW)
    assert loaded == expected
    assert projection.summaries(loaded, NOW) == original.summaries(expected, NOW)
    missing = PublishedHistoryCache(
        ReadPublishedHistoryUseCase(SQLiteQfqWindowCache(tmp_path / "missing", "v2")),
        lookback_sessions=251,
        outcome_history=reader,
    )
    assert missing.read_outcome_bars(("600001",), NOW) == original.read_outcome_bars(("600001",), NOW)
    assert missing.load(("600001",), observed_at=NOW) == {}


def test_runner_lock_blocks_network_and_local_seeding_is_resumable(tmp_path) -> None:
    from tests.component.test_candidate_history_tail import NOW, _Archive

    history = _Archive(251, 0, ("600001", "600002"))
    supplier = Supplier()
    updater = _updater(tmp_path, supplier)
    runner = QfqUpdateRunner(
        ReadPublishedHistoryUseCase(history),
        updater,
        updater.v2,
        updater.v3,
        tmp_path / ".lock",
        lambda: NOW,
    )
    with HistoryMaintenanceLock(tmp_path / ".lock"):
        result = runner.execute()
        assert result.failure_reason is not None
    assert supplier.calls == []
    result = runner.execute(seed_only=True)
    assert result.completed_codes == 2 and result.changed_rows == 624
    before = (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3"))
    assert runner.execute(seed_only=True).changed_files == ()
    assert (_fingerprints(tmp_path / "v2"), _fingerprints(tmp_path / "v3")) == before
    assert supplier.calls == []


@pytest.mark.parametrize("pending,exit_code", ((0, 0), (2, 1)))
def test_qfq_cli_is_zero_argument_and_reports_pending(tmp_path, monkeypatch, capsys, pending, exit_code) -> None:
    calls = []

    def execute(root, *, report):
        calls.append(root)
        report("qfq fixture progress")
        return QfqUpdateResult(DAYS[-1], pending_codes=pending)

    monkeypatch.setattr(cli, "load_runtime_settings", lambda _path: SimpleNamespace(project_root=tmp_path))
    monkeypatch.setattr("trader.bootstrap.execute_qfq_download", execute)
    assert cli.main(["--config", str(tmp_path / "runtime.json"), "qfq_download"]) == exit_code
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == "qfq fixture progress\n"
    assert payload["pending_codes"] == pending
    assert calls == [tmp_path]
    with pytest.raises(SystemExit) as rejected:
        cli.main(["--config", str(tmp_path / "runtime.json"), "qfq_download", "--profile", "v3"])
    assert rejected.value.code == 2
    assert calls == [tmp_path]


def test_local_extraction_heartbeat_is_visible_and_thread_stops() -> None:
    import threading

    from trader.download.infra.qfq_progress import qfq_local_progress

    feedback = threading.Event()
    messages = []

    def report(message):
        messages.append(message)
        if "waiting elapsed=" in message:
            feedback.set()

    with qfq_local_progress(report, interval_seconds=0.01):
        assert messages[0] == "qfq history extraction: started"
        assert feedback.wait(2)
    assert "finished elapsed=" in messages[-1]
    assert not any(thread.name == "qfq-extraction-progress" for thread in threading.enumerate())
