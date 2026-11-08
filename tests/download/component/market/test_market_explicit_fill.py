"""Explicit market repair derives missing facts without manufacturing price gaps."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.qfq import QFQ_CONFIGURATION, qfq_window
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.application.update_qfq import UpdateQfqWindows
from trader.download.domain.baostock_daily import BaoStockCalendar, BaoStockSecurity, BaoStockSourceVersions
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.price_repair import PriceIssue
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow
from trader.download.domain.qfq_planning import QfqNonTradingFact
from trader.download.infra.qfq_sqlite import SQLiteQfqWindowCache
from trader.infra.sqlite_connection import SqliteSession

_DAYS = tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(15))
_CONFIGURATION = replace(QFQ_CONFIGURATION, v2_sessions=15, v3_sessions=5)
_VERSIONS = BaoStockSourceVersions("explicit-fill-fixture", "3.11", ())
_SOURCE = f"{_VERSIONS.sdk_version}:{_VERSIONS.content_hash}"
_NOW = datetime(2025, 1, 15, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def _window(code: str = "600001") -> PublishedHistoryWindow:
    return PublishedHistoryWindow(code, tuple(cell for cell in qfq_window(code).cells if cell.trade_date in _DAYS))


def _one_price(cell: PublishedHistoryCell, *, known_change: bool) -> PublishedHistoryCell:
    assert cell.unadjusted is not None and cell.qfq is not None
    raw = replace(
        cell.unadjusted,
        open_price=10.0, high_price=10.0, low_price=10.0, close_price=10.0,
        preclose=9.99 if known_change else None,
        pct_change=0.1 if known_change else None,
    )
    qfq = replace(cell.qfq, open_price=10.0, high_price=10.0, low_price=10.0, close_price=10.0)
    return replace(cell, unadjusted=raw, qfq=qfq)


@dataclass
class _Supplier:
    candidate: PublishedHistoryWindow = field(default_factory=_window)
    calls: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)
    failure: bool = False
    codes: tuple[str, ...] = ("600001",)

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        assert as_of == _DAYS[-1] and sessions == 15
        securities = tuple(
            BaoStockSecurity(code, "fixture", "main", _DAYS[0], None, "fixture") for code in self.codes
        )
        return HistorySupplierContext(BaoStockCalendar(_DAYS), securities, _VERSIONS)

    def fetch_window(self, security, dates, *, request_budget=None):
        pytest.fail("explicit selected repairs must not first probe Tencent")

    def fetch_baostock_window(self, security, dates):
        self.calls.append((security.code, dates))
        if self.failure:
            raise TimeoutError("fixture_timeout")
        return PublishedHistoryWindow(security.code, tuple(
            replace(
                cell,
                code=security.code,
                unadjusted=replace(cell.unadjusted, code=security.code) if cell.unadjusted is not None else None,
                qfq=replace(cell.qfq, code=security.code) if cell.qfq is not None else None,
            ) for cell in self.candidate.cells
        ))


def _updater(root: Path, supplier: _Supplier) -> UpdateQfqWindows:
    path = root / "market.sqlite3"
    policy = SqlitePolicyFixture()
    return UpdateQfqWindows(
        SQLiteQfqWindowCache(path, "v2", sqlite_policy=policy),
        SQLiteQfqWindowCache(path, "v3", sqlite_policy=policy),
        supplier, lambda: False, lambda _message: None, _CONFIGURATION,
        repair_supplier=supplier, repair_only=True,
    )


def _seed(updater: UpdateQfqWindows, window: PublishedHistoryWindow, *, source: str = _SOURCE) -> None:
    facts = tuple(
        QfqNonTradingFact(window.code, cell.trade_date, "inferred", _DAYS[-1], source)
        for cell in window.cells if cell.status == "tencent_inferred_suspended"
    )
    updater.v2.replace_paired_windows(
        updater.v3, (window,), source, _CONFIGURATION,
        calendar=_DAYS, completed_day=_DAYS[-1], nontrading_facts=facts,
    )


@pytest.mark.parametrize("kind", ("inferred", "raw_change", "both"))
@pytest.mark.parametrize("already_baostock", (False, True))
def test_explicit_fill_repairs_derived_needs_once_and_reuses_completed_window(
    tmp_path: Path, kind: str, already_baostock: bool,
) -> None:
    original = _window()
    candidate = original
    if kind in {"raw_change", "both"}:
        original = replace(original, cells=tuple(
            _one_price(cell, known_change=False) if cell.trade_date == _DAYS[2] else cell
            for cell in original.cells
        ))
        candidate = replace(candidate, cells=tuple(
            _one_price(cell, known_change=True) if cell.trade_date == _DAYS[2] else cell
            for cell in candidate.cells
        ))
    if kind in {"inferred", "both"}:
        original = replace(original, cells=tuple(
            replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
            if cell.trade_date == _DAYS[4] else cell for cell in original.cells
        ))
    supplier = _Supplier(candidate)
    updater = _updater(tmp_path, supplier)
    _seed(updater, original, source=f"baostock:{_SOURCE}" if already_baostock else _SOURCE)
    assert updater.v2.price_issues() == ()

    result = updater.execute(_NOW)

    assert result.pending_codes == 0 and result.completed_codes == 1
    assert result.unresolved_supplement_codes == 0
    assert supplier.calls == [("600001", _DAYS)]
    assert updater.v2.read_code("600001") == candidate
    assert updater.v3.read_code("600001").cells == candidate.cells[-5:]
    assert updater.v2.source_identity("600001") == f"baostock:{_SOURCE}"
    assert updater.v2.repair_supplements(frozenset({"600001"}), _DAYS) == ()
    assert updater.v2.price_issues() == ()
    assert updater.v2.planning_snapshot(frozenset({"600001"})).metadata[0].nontrading == ()

    again = updater.execute(_NOW)
    assert again.pending_codes == again.completed_codes == 0
    assert supplier.calls == [("600001", _DAYS)]


def test_supplement_does_not_reopen_or_rewrite_resolved_core_attempt(tmp_path: Path) -> None:
    original = _window()
    candidate = replace(original, cells=tuple(
        _one_price(cell, known_change=True) if cell.trade_date == _DAYS[2] else cell
        for cell in original.cells
    ))
    original = replace(original, cells=tuple(
        _one_price(cell, known_change=False) if cell.trade_date == _DAYS[2] else cell
        for cell in original.cells
    ))
    supplier = _Supplier(candidate)
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)
    issue = PriceIssue("600001", (_DAYS[2],), "previous_core_gap")
    updater.v2.record_price_issue(issue)
    assert updater.v2.reserve_repair(issue, "previous_core_batch", explicit=True)
    _seed(updater, original)
    with SqliteSession(tmp_path / "market.sqlite3") as connection:
        before = tuple(connection.run("SELECT * FROM price_repair_attempt").require_one())
    assert before[3] == "resolved" and before[-1] == "tencent"

    result = updater.execute(_NOW)

    assert result.completed_codes == 1 and result.pending_codes == 0
    with SqliteSession(tmp_path / "market.sqlite3") as connection:
        assert tuple(connection.run("SELECT * FROM price_repair_attempt").require_one()) == before
    assert supplier.calls == [("600001", _DAYS)]


def test_supplement_failure_preserves_prices_and_retries_explicitly(tmp_path: Path) -> None:
    original = _window()
    original = replace(original, cells=tuple(
        replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
        if cell.trade_date == _DAYS[4] else cell for cell in original.cells
    ))
    supplier = _Supplier(failure=True)
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)
    before = updater.v2.manifest()
    failures = []
    updater = replace(updater, failures=lambda context, error: failures.append((context, error)))

    result = updater.execute(_NOW)

    assert result.pending_codes == 1 and result.completed_codes == 0
    assert result.unresolved_supplement_codes == 1 and result.latest_missing_codes == 0
    assert updater.v2.manifest() == before
    assert updater.v2.price_issues() == ()
    assert len(failures) == 1
    with SqliteSession(tmp_path / "market.sqlite3") as connection:
        assert connection.run("SELECT COUNT(*) FROM price_repair_attempt").require_one()[0] == 0
    supplier.failure = False
    assert updater.execute(_NOW).pending_codes == 0
    assert supplier.calls == [("600001", _DAYS), ("600001", _DAYS)]


def test_supplement_source_that_still_omits_change_remains_pending(tmp_path: Path) -> None:
    original = _window()
    original = replace(original, cells=tuple(
        _one_price(cell, known_change=False) if cell.trade_date == _DAYS[2] else cell
        for cell in original.cells
    ))
    supplier = _Supplier(original)
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)

    first = updater.execute(_NOW)
    second = updater.execute(_NOW)

    assert first.pending_codes == second.pending_codes == 1
    assert first.unresolved_supplement_codes == second.unresolved_supplement_codes == 1
    assert first.completed_codes == second.completed_codes == 0
    assert supplier.calls == [("600001", _DAYS), ("600001", _DAYS)]
    assert updater.v2.price_issues() == ()


def test_paired_publication_rollback_keeps_old_window_and_next_fill_refetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _window()
    original = replace(original, cells=tuple(
        replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
        if cell.trade_date == _DAYS[4] else cell for cell in original.cells
    ))
    supplier = _Supplier()
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)
    before = (updater.v2.manifest(), updater.v3.manifest())
    publish = updater.v3._publish_identity
    failed = False

    def interrupted(connection):
        nonlocal failed
        if not failed:
            failed = True
            raise sqlite3.OperationalError("fixture_publication_failure")
        return publish(connection)

    monkeypatch.setattr(updater.v3, "_publish_identity", interrupted)
    first = updater.execute(_NOW)

    assert first.pending_codes == 1
    assert (updater.v2.manifest(), updater.v3.manifest()) == before
    assert updater.v2.price_issues() == ()
    assert updater.execute(_NOW).pending_codes == 0
    assert supplier.calls == [("600001", _DAYS), ("600001", _DAYS)]


def test_ordinary_null_change_is_not_a_supplement_request(tmp_path: Path) -> None:
    original = _window()
    original = replace(original, cells=tuple(
        replace(cell, unadjusted=replace(cell.unadjusted, preclose=None, pct_change=None))
        for cell in original.cells if cell.unadjusted is not None
    ))
    supplier = _Supplier()
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)

    assert updater.execute(_NOW).pending_codes == 0
    assert supplier.calls == []


def test_core_gap_and_supplement_use_one_supplier_candidate(tmp_path: Path) -> None:
    complete = _window()
    candidate = replace(complete, cells=tuple(
        _one_price(cell, known_change=True) if cell.trade_date == _DAYS[2] else cell
        for cell in complete.cells
    ))
    original = replace(complete, cells=tuple(
        _one_price(cell, known_change=False) if cell.trade_date == _DAYS[2] else cell
        for cell in complete.cells if cell.trade_date != _DAYS[4]
    ))
    supplier = _Supplier(candidate)
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)
    updater.v2.record_price_issue(PriceIssue("600001", (_DAYS[4],), "fixture_core_gap"))

    result = updater.execute(_NOW)

    assert result.pending_codes == 0 and result.completed_codes == 1
    assert supplier.calls == [("600001", _DAYS)]
    assert updater.v2.price_issues() == ()
    assert updater.v2.read_code("600001") == candidate


@pytest.mark.parametrize("missing_field", ("preclose", "pct_change", "turnover"))
def test_candidate_reference_cannot_hide_known_published_raw_fields(tmp_path: Path, missing_field: str) -> None:
    complete = _window()
    original = replace(complete, cells=tuple(
        replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
        if cell.trade_date == _DAYS[4] else cell for cell in complete.cells
    ))
    candidate = replace(complete, cells=tuple(
        replace(cell, unadjusted=replace(cell.unadjusted, **{missing_field: None}))
        if cell.trade_date == _DAYS[2] and cell.unadjusted is not None else cell for cell in complete.cells
    ))
    supplier = _Supplier(candidate)
    updater = _updater(tmp_path, supplier)
    _seed(updater, original)
    updater.v2.record_price_issue(PriceIssue("600001", (_DAYS[4],), "fixture_core_gap"), candidate)
    before = (updater.v2.manifest(), updater.v3.manifest())
    published = (updater.v2.read_code("600001"), updater.v3.read_code("600001"))
    failures = []
    updater = replace(updater, failures=lambda context, error: failures.append((context, error)))

    result = updater.execute(_NOW)

    assert result.pending_codes == 1 and result.completed_codes == 0
    assert result.unresolved_supplement_codes == 1
    assert supplier.calls == [("600001", _DAYS)]
    assert (updater.v2.manifest(), updater.v3.manifest()) == before
    assert (updater.v2.read_code("600001"), updater.v3.read_code("600001")) == published
    assert len(failures) == 1 and str(failures[0][1]) == "price_replacement_known_raw_field_missing"


def test_cancelled_supplement_does_not_publish_late_source_result(tmp_path: Path) -> None:
    original = _window()
    original = replace(original, cells=tuple(
        replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
        if cell.trade_date == _DAYS[4] else cell for cell in original.cells
    ))
    cancelled = False

    class _LateSupplier(_Supplier):
        def fetch_baostock_window(self, security, dates):
            nonlocal cancelled
            candidate = super().fetch_baostock_window(security, dates)
            cancelled = True
            return candidate

    supplier = _LateSupplier()
    updater = replace(_updater(tmp_path, supplier), cancel_requested=lambda: cancelled)
    _seed(updater, original)
    before = (updater.v2.manifest(), updater.v3.manifest())

    result = updater.execute(_NOW)

    assert result.failure_reason == "cancelled" and result.pending_codes >= 1
    assert (updater.v2.manifest(), updater.v3.manifest()) == before
    assert supplier.calls == [("600001", _DAYS)]


def test_scoped_fill_keeps_unselected_prices_population_and_pending_needs(tmp_path: Path) -> None:
    codes = ("600001", "600002", "600003")
    supplier = _Supplier(codes=codes)
    updater = replace(_updater(tmp_path, supplier), repair_codes=frozenset({"600001"}))
    for code in codes:
        window = _window(code)
        if code != "600003":
            window = replace(window, cells=tuple(
                replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                if cell.trade_date == _DAYS[2] else cell for cell in window.cells
                if code != "600002" or cell.trade_date != _DAYS[4]
            ))
        _seed(updater, window)
    updater.v2.record_price_issue(PriceIssue("600002", (_DAYS[4],), "fixture_core_gap"))
    before = tuple((updater.v2.read_code(code), updater.v3.read_code(code)) for code in codes[1:])
    with SqliteSession(tmp_path / "market.sqlite3") as connection:
        bindings = tuple(connection.run("SELECT * FROM window_binding WHERE code<>'600001' ORDER BY profile,code"))
        calendar = tuple(connection.run("SELECT * FROM window_calendar ORDER BY profile,trade_date"))
        population = tuple(connection.run("SELECT * FROM window_population ORDER BY profile,code"))

    result = updater.execute(_NOW)

    assert supplier.calls == [("600001", _DAYS)]
    assert result.completed_codes == 1 and result.pending_codes == 1
    assert result.unresolved_supplement_codes == 1 and result.needs_retry
    assert updater.v2.read_code("600001") == _window()
    assert before == tuple((updater.v2.read_code(code), updater.v3.read_code(code)) for code in codes[1:])
    with SqliteSession(tmp_path / "market.sqlite3") as connection:
        assert tuple(connection.run("SELECT * FROM window_binding WHERE code<>'600001' ORDER BY profile,code")) == bindings
        assert tuple(connection.run("SELECT * FROM window_calendar ORDER BY profile,trade_date")) == calendar
        assert tuple(connection.run("SELECT * FROM window_population ORDER BY profile,code")) == population
    assert {issue.code for issue in updater.v2.price_issues()} == {"600002"}
    again = updater.execute(_NOW)
    assert again.completed_codes == 0 and again.pending_codes == 1
    assert again.unresolved_supplement_codes == 1 and again.needs_retry
    assert supplier.calls == [("600001", _DAYS)]
    assert replace(updater, repair_codes=None).execute(_NOW).pending_codes == 0
    assert supplier.calls == [("600001", _DAYS), ("600002", _DAYS)]


def test_scope_outside_current_eligible_universe_is_rejected_before_publication(tmp_path: Path) -> None:
    supplier = _Supplier()
    updater = replace(_updater(tmp_path, supplier), repair_codes=frozenset({"600002"}))
    _seed(updater, _window())
    before = (updater.v2.manifest(), updater.v3.manifest())

    with pytest.raises(ValueError, match="qfq_repair_codes_not_eligible"):
        updater.execute(_NOW)

    assert supplier.calls == []
    assert (updater.v2.manifest(), updater.v3.manifest()) == before


@pytest.mark.parametrize("codes", (frozenset(), frozenset({"60000"}), frozenset(f"60000{index}" for index in range(6))))
def test_invalid_repair_scope_is_rejected(tmp_path: Path, codes: frozenset[str]) -> None:
    with pytest.raises(ValueError, match="qfq_repair_codes_invalid"):
        replace(_updater(tmp_path, _Supplier()), repair_codes=codes)


def test_repair_scope_requires_explicit_fill(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="qfq_repair_codes_require_fill"):
        replace(_updater(tmp_path, _Supplier()), repair_only=False, repair_codes=frozenset({"600001"}))
