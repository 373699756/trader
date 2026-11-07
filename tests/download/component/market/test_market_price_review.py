"""Market provenance and bounded review checks using isolated SQLite copies."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.download.qfq import QFQ_CONFIGURATION, qfq_window
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSecurity, BaoStockSourceVersions
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.price_repair import PriceIssue
from trader.download.domain.price_review import PriceReviewBudgetExhausted, PriceReviewConfiguration, PriceReviewRequestBudget
from trader.download.domain.published_history import PublishedHistoryWindow
from trader.download.domain.qfq_planning import QfqNonTradingFact
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.download.infra.tencent_request_budget import TencentRequestBudget
from trader.infra.sqlite_codec import EncodeDate
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnectionOptions, SqliteTransaction

_DAYS = tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(25))
_VERSIONS = BaoStockSourceVersions("fixture", "3.11", ())
_SOURCE = f"{_VERSIONS.sdk_version}:{_VERSIONS.content_hash}"


def _observed(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


@dataclass
class _Supplier:
    days: tuple[date, ...] = _DAYS[:15]
    codes: tuple[str, ...] = ("600001",)
    suspended_on: date | None = None
    suspend_all: bool = False
    inferred_on: date | None = None
    resolve_on_review: bool = False
    requests_per_review: int = 1
    calls: list[tuple[str, tuple[date, ...], bool]] = field(default_factory=list)

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        return HistorySupplierContext(
            BaoStockCalendar(self.days),
            tuple(BaoStockSecurity(code, "fixture", "main", _DAYS[0], None, "fixture") for code in self.codes),
            _VERSIONS,
        )

    def fetch_window(
        self, security: BaoStockSecurity, dates: tuple[date, ...], *, request_budget: PriceReviewRequestBudget | None = None
    ) -> PublishedHistoryWindow:
        self.calls.append((security.code, dates, request_budget is not None))
        if request_budget is not None:
            for _ in range(self.requests_per_review):
                request_budget.reserve_request(10.0)
        cells = []
        for cell in qfq_window(security.code).cells:
            if cell.trade_date not in dates:
                continue
            if cell.trade_date == self.suspended_on or self.suspend_all:
                cell = QfqNonTradingFact(security.code, cell.trade_date, "confirmed", self.days[-1], _SOURCE).cell()
            elif cell.trade_date == self.inferred_on and not (request_budget is not None and self.resolve_on_review):
                cell = replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
            cells.append(cell)
        return PublishedHistoryWindow(security.code, tuple(cells))


def _updater(root: Path, supplier: _Supplier, **options) -> UpdateQfqWindows:
    policy = SqlitePolicyFixture()
    path = root / "market.sqlite3"
    return UpdateQfqWindows(
        SQLiteQfqWindowCache(path, "v2", sqlite_policy=policy),
        SQLiteQfqWindowCache(path, "v3", sqlite_policy=policy),
        supplier, lambda: False, lambda _message: None, QFQ_CONFIGURATION,
        **options,
    )


@pytest.mark.parametrize("whole_window", (False, True))
def test_confirmed_latest_reuses_target_day_across_observation_days_and_fetches_new_target(tmp_path: Path, whole_window: bool) -> None:
    supplier = _Supplier(suspended_on=_DAYS[14], suspend_all=whole_window)
    updater = _updater(tmp_path, supplier)
    first = updater.execute(_observed(_DAYS[14]))
    assert first.latest_suspended_codes == 1
    assert len(supplier.calls) == 1
    prices = updater.v2.read_code("600001").cells
    assert prices == () if whole_window else prices[-1].trade_date == _DAYS[13]
    snapshot = updater.v2.planning_snapshot(frozenset(supplier.codes))
    assert snapshot.metadata[0].nontrading[-1] == QfqNonTradingFact("600001", _DAYS[14], "confirmed", _DAYS[14], _SOURCE)
    assert updater.execute(_observed(_DAYS[14])).skipped_codes == 1
    assert len(supplier.calls) == 1
    assert updater.execute(_observed(_DAYS[15])).skipped_codes == 1
    assert len(supplier.calls) == 1
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[-1].checked_on == _DAYS[14]
    supplier.days = _DAYS[:16]
    assert updater.execute(_observed(_DAYS[15])).skipped_codes == 0
    assert len(supplier.calls) == 2
    assert _DAYS[15] in updater.v2.covered_dates("600001")


@pytest.mark.parametrize("revised_qfq", (False, True))
@pytest.mark.parametrize("halt_days", (1, 3))
def test_confirmed_suspension_survives_next_day_tail_and_full_without_inferred_review(tmp_path: Path, revised_qfq: bool, halt_days: int) -> None:
    class _NextDaySupplier(_Supplier):
        revision = False
        historical_halts: tuple[date, ...] = ()
        infer_halts = False

        def fetch_window(self, security, dates, *, request_budget=None):
            window = super().fetch_window(security, dates, request_budget=request_budget)
            window = replace(window, cells=tuple(
                QfqNonTradingFact(cell.code, cell.trade_date, "inferred" if self.infer_halts else "confirmed", self.days[-1], _SOURCE).cell()
                if cell.trade_date in self.historical_halts else cell for cell in window.cells
            ))
            if not self.revision:
                return window
            return replace(window, cells=tuple(
                replace(cell, qfq=replace(cell.qfq, open_price=9.0, high_price=9.18, low_price=8.82, close_price=9.0))
                if cell.status == "complete" and cell.qfq is not None else cell
                for cell in window.cells
            ))

    supplier = _NextDaySupplier()
    supplier.historical_halts = _DAYS[15 - halt_days:15]
    updater = _updater(tmp_path, supplier)
    updater.execute(_observed(_DAYS[14]))
    supplier.infer_halts = True
    supplier.days = _DAYS[:16]
    supplier.revision = revised_qfq
    before = len(supplier.calls)
    budget = TencentRequestBudget(monotonic=lambda: 0.0)
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[15]))
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert len(supplier.calls) - before == (2 if revised_qfq or halt_days == 3 else 1)
    facts = updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading
    assert facts == tuple(QfqNonTradingFact("600001", day, "confirmed", _DAYS[14], _SOURCE) for day in supplier.historical_halts)
    assert updater.v2.read_code("600001").cells[-1].trade_date == _DAYS[15]
    assert budget.request_count == result.price_review.examined_codes == 0
    assert result.price_review.remaining_inferred_codes == 0
    calls = len(supplier.calls)
    assert replace(updater, price_review_budget=TencentRequestBudget(monotonic=lambda: 0.0)).execute(_observed(_DAYS[15])).skipped_codes == 1
    assert len(supplier.calls) == calls

    # A later actual price is still adopted through the existing whole-window
    # replacement path, and removes the earlier nontrading observation.
    supplier.historical_halts = ()
    supplier.days = _DAYS[:17]
    completed = replace(updater, price_review_budget=TencentRequestBudget(monotonic=lambda: 0.0)).execute(_observed(_DAYS[16]))
    assert completed.completed_codes == 1 and completed.pending_codes == 0
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading == ()
    assert _DAYS[14] in {cell.trade_date for cell in updater.v2.read_code("600001").cells}


def test_future_confirmed_observation_is_not_reused(tmp_path: Path) -> None:
    supplier = _Supplier(suspended_on=_DAYS[14])
    updater = _updater(tmp_path, supplier)
    window = supplier.fetch_window(supplier.load_qfq_context(_DAYS[14], 251).universe[0], supplier.days)
    updater.v2.replace_paired_windows(updater.v3, (window,), _SOURCE, QFQ_CONFIGURATION, calendar=supplier.days, completed_day=_DAYS[14], nontrading_facts=(QfqNonTradingFact("600001", _DAYS[14], "confirmed", _DAYS[16], _SOURCE),))
    supplier.calls.clear()
    result = updater.execute(_observed(_DAYS[14]))
    assert result.skipped_codes == 0 and result.completed_codes == 1 and len(supplier.calls) == 1
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0].checked_on == _DAYS[14]


def test_inferred_review_waits_five_sessions_and_follows_ordinary_download(tmp_path: Path) -> None:
    supplier = _Supplier(inferred_on=_DAYS[10], resolve_on_review=True)
    updater = _updater(tmp_path, supplier, price_review_budget=TencentRequestBudget())
    updater.execute(_observed(_DAYS[14]))
    assert updater.execute(_observed(_DAYS[14])).skipped_codes == 1
    assert len(supplier.calls) == 1
    supplier.days = _DAYS[:19]
    replace(updater, price_review_budget=TencentRequestBudget()).execute(_observed(_DAYS[18]))
    assert len(supplier.calls) == 2
    assert not any(review for _, _, review in supplier.calls)
    facts = updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading
    assert facts[0].checked_on == _DAYS[14]
    supplier.days = _DAYS[:20]
    budget = TencentRequestBudget()
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[19]))
    assert result.pending_codes == 0 and result.completed_codes == 1
    assert [review for _, _, review in supplier.calls] == [False, False, False, True]
    assert budget.request_count == 1
    assert result.price_review.examined_codes == result.price_review.physical_requests == 1
    assert result.price_review.remaining_inferred_codes == 0
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading == ()
    assert _DAYS[10] in {cell.trade_date for cell in updater.v2.read_code("600001").cells}


def test_ordinary_tail_observation_prevents_duplicate_inferred_review(tmp_path: Path) -> None:
    supplier = _Supplier(inferred_on=_DAYS[12])
    updater = _updater(tmp_path, supplier)
    updater.execute(_observed(_DAYS[14]))
    supplier.days = _DAYS[:20]
    budget = TencentRequestBudget()
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[19]))
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert len(supplier.calls) == 2 and budget.request_count == 0
    assert result.price_review.examined_codes == 0 and result.price_review.remaining_inferred_codes == 1
    fact = updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0]
    assert fact.checked_on == _DAYS[19]


def test_budget_exhaustion_preserves_facts_without_supplier_failure(tmp_path: Path) -> None:
    supplier = _Supplier(inferred_on=_DAYS[10], requests_per_review=2)
    failures = []
    updater = _updater(tmp_path, supplier, failures=lambda context, error: failures.append((context, error)))
    updater.execute(_observed(_DAYS[14]))
    supplier.days = _DAYS[:20]
    budget = TencentRequestBudget(PriceReviewConfiguration(maximum_requests=1))
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[19]))
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert failures == [] and updater.v2.price_issues() == ()
    assert budget.request_count == 1 and budget.exhausted_reason == "request_limit"
    assert result.price_review.exhaustion_reason == "request_limit"
    assert result.price_review.remaining_inferred_codes == 1
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0].checked_on == _DAYS[14]
    assert updater.v2.read_code("600001").cells[-1].trade_date == _DAYS[19]


def test_review_caps_codes_and_physical_requests(tmp_path: Path) -> None:
    supplier = _Supplier(days=_DAYS[:20], codes=tuple(f"600{number:03d}" for number in range(10)), inferred_on=_DAYS[10], requests_per_review=2)
    updater = _updater(tmp_path, supplier)
    windows = tuple(supplier.fetch_window(security, supplier.days) for security in supplier.load_qfq_context(_DAYS[19], 251).universe)
    facts = tuple(QfqNonTradingFact(code, _DAYS[10], "inferred", _DAYS[14], _SOURCE) for code in supplier.codes)
    updater.v2.replace_paired_windows(updater.v3, windows, _SOURCE, QFQ_CONFIGURATION, calendar=supplier.days, completed_day=_DAYS[19], nontrading_facts=facts)
    supplier.calls.clear()
    # Code/request limits are deterministic; the separate SQL test advances time.
    budget = TencentRequestBudget(monotonic=lambda: 0.0)
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[19]))
    assert result.completed_codes == 10 and result.pending_codes == 0
    assert len(supplier.calls) == len(budget.reviewed_codes) == 8
    assert budget.request_count == 16
    assert result.price_review.examined_codes == 8 and result.price_review.physical_requests == 16
    assert result.price_review.remaining_inferred_codes == 10
    remaining = updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[-2:]
    assert all(state.nontrading[0].checked_on == _DAYS[14] for state in remaining)


def test_incompatible_provenance_requires_deletion_and_a_fresh_download(tmp_path: Path) -> None:
    source = tmp_path / "sealed"
    supplier = _Supplier(suspended_on=_DAYS[14])
    updater = _updater(source, supplier)
    updater.execute(_observed(_DAYS[14]))
    with updater.v2._write_connection() as connection:
        connection.run("DROP TABLE price_nontrading")
        connection.run("CREATE TABLE price_nontrading(code TEXT NOT NULL,trade_date INTEGER NOT NULL,PRIMARY KEY(code,trade_date)) WITHOUT ROWID")
        connection.run("INSERT INTO price_nontrading VALUES(?,?)", ("600001", EncodeDate(_DAYS[14])))
    before = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        updater.v2.planning_snapshot(frozenset(supplier.codes))
    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        updater.v2.select_published_codes(updater.v3, frozenset(supplier.codes))
    assert {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()} == before
    # Only this isolated fixture is discarded; ordinary reads and writes never delete it.
    updater.v2.path.unlink()
    Path(f"{updater.v2.path}.sha256").unlink()
    supplier.calls.clear()
    fresh = _updater(source, supplier)
    result = fresh.execute(_observed(_DAYS[14]))
    assert result.failure_reason is None and len(supplier.calls) == 1
    fact = fresh.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0]
    assert fact.kind == "confirmed"


def test_discovery_sql_deadline_is_not_a_supplier_failure(tmp_path: Path, monkeypatch) -> None:
    from trader.infra.sqlite_connection import SqliteConnection

    supplier = _Supplier(inferred_on=_DAYS[10])
    failures = []
    updater = _updater(tmp_path, supplier, failures=lambda context, error: failures.append((context, error)))
    updater.execute(_observed(_DAYS[14]))
    clock = [0.0]
    budget = TencentRequestBudget(monotonic=lambda: clock[0])
    original = SqliteConnection.observe_progress

    def expire_during_sql(connection, callback, steps) -> None:
        if callback is not None and steps == 1000:
            def expire() -> int:
                clock[0] = 16.0
                return callback()
            original(connection, expire, 1)
        else:
            original(connection, callback, steps)

    monkeypatch.setattr(SqliteConnection, "observe_progress", expire_during_sql)
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[14]))
    assert not result.price_review.discovery_completed
    assert result.price_review.exhaustion_reason == "deadline"
    assert result.price_review.examined_codes == result.price_review.physical_requests == 0
    assert result.completed_codes == 1 and result.pending_codes == 0 and failures == []
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0].checked_on == _DAYS[14]


def test_exhausted_shared_requests_skip_market_discovery(tmp_path: Path, monkeypatch) -> None:
    supplier = _Supplier(inferred_on=_DAYS[10])
    updater = _updater(tmp_path, supplier)
    updater.execute(_observed(_DAYS[14]))
    budget = TencentRequestBudget()
    for _ in range(16):
        budget.reserve_request(10.0)
    budget.pause()

    def unexpected_discovery(*args, **kwargs):
        raise AssertionError("spent shared budget must not start market discovery")

    monkeypatch.setattr(updater.v2, "review_states", unexpected_discovery)
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[14]))
    assert not result.price_review.discovery_completed
    assert result.price_review.exhaustion_reason == "request_limit"
    assert result.price_review.physical_requests == 0 and budget.request_count == 16


def test_successful_review_of_existing_all_inferred_updates_check_age(tmp_path: Path) -> None:
    supplier = _Supplier(days=_DAYS[:20])
    updater = _updater(tmp_path, supplier)
    facts = tuple(QfqNonTradingFact("600001", day, "inferred", max(day, _DAYS[14]), _SOURCE) for day in supplier.days)
    window = PublishedHistoryWindow("600001", tuple(fact.cell() for fact in facts))
    updater.v2.replace_paired_windows(updater.v3, (window,), _SOURCE, QFQ_CONFIGURATION, calendar=supplier.days, completed_day=_DAYS[19], nontrading_facts=facts)

    class _AllInferred(_Supplier):
        def fetch_window(self, security, dates, *, request_budget=None):
            self.calls.append((security.code, dates, request_budget is not None))
            request_budget.reserve_request(10.0)
            return PublishedHistoryWindow(security.code, tuple(QfqNonTradingFact(security.code, day, "inferred", _DAYS[19], _SOURCE).cell() for day in dates))

    reviewing = _AllInferred(days=supplier.days)
    result = replace(updater, supplier=reviewing, price_review_budget=TencentRequestBudget()).execute(_observed(_DAYS[19]))
    assert result.price_review.examined_codes == result.price_review.physical_requests == 1
    assert result.price_review.remaining_inferred_codes == 1
    assert all(fact.checked_on == _DAYS[19] for fact in updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading)
    assert updater.v2.read_code("600001").cells == ()
    replace(updater, supplier=reviewing, price_review_budget=TencentRequestBudget()).execute(_observed(_DAYS[19]))
    assert len(reviewing.calls) == 1


def test_baostock_inferred_review_cannot_use_unbudgeted_cooling_probe(tmp_path: Path) -> None:
    supplier = _Supplier(days=_DAYS[:20], inferred_on=_DAYS[10], resolve_on_review=True, requests_per_review=2)
    failures = []
    updater = _updater(tmp_path, supplier, failures=lambda context, error: failures.append((context, error)))
    window = supplier.fetch_window(supplier.load_qfq_context(_DAYS[19], 251).universe[0], supplier.days)
    source = f"baostock:{_SOURCE}"
    updater.v2.replace_paired_windows(updater.v3, (window,), source, QFQ_CONFIGURATION, calendar=supplier.days, completed_day=_DAYS[19], nontrading_facts=(QfqNonTradingFact("600001", _DAYS[10], "inferred", _DAYS[14], source),))
    supplier.calls.clear()
    budget = TencentRequestBudget(PriceReviewConfiguration(maximum_requests=1))
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[19]))
    assert len(supplier.calls) == 1 and supplier.calls[0][2]
    assert budget.request_count == 1 and result.price_review.exhaustion_reason == "request_limit"
    assert failures == [] and updater.v2.source_identity("600001") == source


def _seed_current_inferred(updater: UpdateQfqWindows, supplier: _Supplier) -> None:
    security = supplier.load_qfq_context(_DAYS[19], 251).universe[0]
    window = supplier.fetch_window(security, supplier.days)
    updater.v2.replace_paired_windows(updater.v3, (window,), _SOURCE, QFQ_CONFIGURATION, calendar=supplier.days, completed_day=_DAYS[19], nontrading_facts=(QfqNonTradingFact("600001", _DAYS[10], "inferred", _DAYS[14], _SOURCE),))
    supplier.calls.clear()


def test_review_write_deadline_rolls_back_facts_and_completion(tmp_path: Path, monkeypatch) -> None:
    from trader.infra.sqlite_connection import SqliteConnection

    supplier = _Supplier(days=_DAYS[:20], inferred_on=_DAYS[10])
    failures = []
    updater = _updater(tmp_path, supplier, failures=lambda context, error: failures.append((context, error)))
    _seed_current_inferred(updater, supplier)
    previous = updater.v2.read_code("600001")
    clock = [0.0]
    budget = TencentRequestBudget(monotonic=lambda: clock[0])
    original = SqliteConnection.run_many

    def expire_after_fact_write(connection, sql, parameters):
        result = original(connection, sql, parameters)
        if sql.startswith("INSERT INTO price_nontrading"):
            clock[0] = 16.0
        return result

    monkeypatch.setattr(SqliteConnection, "run_many", expire_after_fact_write)
    result = replace(updater, price_review_budget=budget).execute(_observed(_DAYS[19]))
    assert result.price_review.exhaustion_reason == "deadline"
    assert result.completed_codes == 1 and result.pending_codes == 0
    assert updater.v2.read_code("600001") == previous
    assert updater.v2.completed(_DAYS[19], "600001", _SOURCE)
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0].checked_on == _DAYS[14]
    assert failures == [] and updater.v2.price_issues() == ()


def test_review_lock_contention_uses_one_millisecond_busy_wait_and_keeps_window(tmp_path: Path, monkeypatch) -> None:
    from trader.download.infra import qfq_sqlite as module

    supplier = _Supplier(days=_DAYS[:20], inferred_on=_DAYS[10], resolve_on_review=True)
    failures = []
    updater = _updater(tmp_path, supplier, failures=lambda context, error: failures.append((context, error)))
    _seed_current_inferred(updater, supplier)
    previous = updater.v2.read_code("600001")
    opened_timeouts = []
    original_open = module.OpenSqliteConnection

    def observe_open(path, options, **kwargs):
        connection = original_open(path, options, **kwargs)
        opened_timeouts.append((options.timeout_seconds, kwargs["sqlite_policy"], connection.run("PRAGMA busy_timeout").require_one()[0]))
        return connection

    monkeypatch.setattr(module, "OpenSqliteConnection", observe_open)
    locker = OpenSqliteConnection(updater.v2.path, SqliteConnectionOptions(timeout_seconds=0))
    transaction = SqliteTransaction(locker, immediate=True)

    class _LockedSupplier(_Supplier):
        def fetch_window(self, security, dates, *, request_budget=None):
            result = super().fetch_window(security, dates, request_budget=request_budget)
            if request_budget is not None:
                transaction.begin()
            return result

    locked = _LockedSupplier(days=supplier.days, inferred_on=_DAYS[10], resolve_on_review=True)
    try:
        result = replace(updater, supplier=locked, price_review_budget=TencentRequestBudget(monotonic=lambda: 0.0)).execute(_observed(_DAYS[19]))
    finally:
        transaction.rollback()
        locker.close()
    assert result.completed_codes == 1 and result.pending_codes == 0
    review_opens = tuple((policy, busy_ms) for configured, policy, busy_ms in opened_timeouts if configured == 0.001)
    assert review_opens and all(0 <= busy_ms <= 1 for _, busy_ms in review_opens)
    assert all(policy == replace(SqlitePolicyFixture(), connection_timeout_seconds=0.001) and policy is not SqlitePolicyFixture() for policy, _ in review_opens)
    assert all(policy is SqlitePolicyFixture() for configured, policy, _ in opened_timeouts if configured != 0.001)
    assert updater.v2.read_code("600001") == previous
    assert updater.v2.completed(_DAYS[19], "600001", _SOURCE)
    assert updater.v2.planning_snapshot(frozenset(supplier.codes)).metadata[0].nontrading[0].checked_on == _DAYS[14]
    assert failures == [] and updater.v2.price_issues() == ()


def test_review_expiry_after_open_closes_connection_without_fallback(tmp_path: Path, monkeypatch) -> None:
    from trader.download.infra import qfq_sqlite as module

    supplier = _Supplier(days=_DAYS[:20], inferred_on=_DAYS[10])
    updater = _updater(tmp_path, supplier)
    _seed_current_inferred(updater, supplier)
    clock = [0.0]
    budget = TencentRequestBudget(monotonic=lambda: clock[0])
    opened = []
    original_open = module.OpenSqliteConnection

    def expire_after_open(path, options, **kwargs):
        connection = original_open(path, options, **kwargs)
        opened.append(connection)
        clock[0] = 16.0
        return connection

    monkeypatch.setattr(module, "OpenSqliteConnection", expire_after_open)
    with pytest.raises(PriceReviewBudgetExhausted, match="price_review_budget_exhausted"):
        updater.v2.read_codes(("600001",), review_budget=budget)
    assert len(opened) == 1 and budget.request_count == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].run("SELECT 1")


@pytest.mark.parametrize("kind", ("confirmed", "inferred"))
def test_seed_explicit_observation_retains_status_source_and_read_date(tmp_path: Path, kind: str) -> None:
    supplier = _Supplier(suspended_on=_DAYS[14] if kind == "confirmed" else None, inferred_on=_DAYS[14] if kind == "inferred" else None)
    updater = _updater(tmp_path, supplier)
    window = supplier.fetch_window(supplier.load_qfq_context(_DAYS[14], 251).universe[0], supplier.days)
    source = "history:" + "a" * 64
    updater.seed((window,), source, calendar=supplier.days, nontrading_observed_on=_DAYS[19])
    snapshot = updater.v2.planning_snapshot(frozenset(supplier.codes))
    fact = snapshot.metadata[0].nontrading[0]
    assert fact.kind == kind and fact.source == source and fact.checked_on == _DAYS[19]
    assert snapshot.windows.states[0].source == source
    assert _DAYS[14] in updater.v2.covered_dates("600001")
    assert updater.v2.read_code("600001").cells[-1].trade_date == _DAYS[13]


def test_unknown_seed_does_not_create_confirmed_facts_and_candidates_agree(tmp_path: Path) -> None:
    supplier = _Supplier(suspended_on=_DAYS[14])
    updater = _updater(tmp_path, supplier)
    window = supplier.fetch_window(supplier.load_qfq_context(_DAYS[14], 251).universe[0], supplier.days)
    updater.seed((window,), _SOURCE, calendar=supplier.days)
    legacy = updater.v2.planning_snapshot(frozenset(supplier.codes))
    assert legacy.metadata[0].nontrading[0].kind == "legacy_unverified"
    assert _DAYS[14] not in updater.v2.covered_dates("600001")
    assert legacy == updater.v2.planning_snapshot(frozenset(supplier.codes), batched=True)
    assert updater.v3.planning_snapshot(frozenset(supplier.codes)) == updater.v3.planning_snapshot(frozenset(supplier.codes), batched=True)
    updater.v2.record_price_issue(PriceIssue("600001", (_DAYS[0], _DAYS[14]), "wanted_fixture"))
    updater.v2.record_price_issue(PriceIssue("600002", (_DAYS[14],), "retired_fixture"))
    assert updater.v2.price_issues(frozenset(("600001",)), (_DAYS[14],)) == (PriceIssue("600001", (_DAYS[14],), "wanted_fixture"),)


def test_cooling_probe_removal_is_disabled_by_default(tmp_path: Path) -> None:
    supplier = _Supplier(days=_DAYS[:15])
    updater = _updater(tmp_path, supplier)
    window = supplier.fetch_window(supplier.load_qfq_context(_DAYS[14], 251).universe[0], supplier.days)
    updater.v2.replace_paired_windows(updater.v3, (window,), f"baostock:{_SOURCE}", QFQ_CONFIGURATION, calendar=supplier.days, completed_day=_DAYS[14])

    class _Repair:
        def fetch_baostock_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
            return supplier.fetch_window(security, dates)

    supplier.days = _DAYS[:16]
    supplier.calls.clear()
    enabled = replace(updater, repair_supplier=_Repair(), skip_cooling_tencent_probe=True)
    result = enabled.execute(_observed(_DAYS[15]))
    assert result.pending_codes == 0
    # One BaoStock tail repair, with no discarded Tencent probe.
    assert len(supplier.calls) == 1
    assert not updater.skip_cooling_tencent_probe and not updater.batch_planning_candidate
