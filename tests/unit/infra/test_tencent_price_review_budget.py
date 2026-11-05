"""Offline request accounting and opt-in Tencent pagination candidates."""

from __future__ import annotations

import json
import asyncio
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.price_review import PriceReviewBudgetExhausted, PriceReviewConfiguration
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqOptions, TencentQfqSupplier
from trader.download.infra.tencent_request_budget import TencentRequestBudget
from trader.infra.http_contracts import HttpCancelled, HttpRequest, HttpResponse, HttpTimeout
from trader.infra.http_transport import RequestsHttpTransport

_DAYS = tuple(date(2018, 1, 1) + timedelta(days=index) for index in range(2800) if index % 7 < 5)
_SECURITY = BaoStockSecurity("600001", "fixture", "main", _DAYS[0], None, "fixture")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Wire:
    def __init__(self, behavior: str = "complete") -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, str, date, int, float]] = []
        self.did_empty = False
        self.advance: Callable[[], None] | None = None

    def __call__(self, request: HttpRequest) -> HttpResponse:
        symbol, _, start, end, count, mode = parse_qs(urlsplit(request.url).query)["param"][0].split(",")
        host = "proxy" if "proxy.finance" in request.url else "direct"
        end_day = date.fromisoformat(end)
        self.calls.append((host, mode, end_day, int(count), request.timeout_seconds))
        if self.advance is not None:
            self.advance()
        older = mode == "qfq" and end_day < _DAYS[-1]
        fail_primary = (
            self.behavior.startswith("resume") and host == "proxy" and older
            and (
                self.behavior != "resume_after_two"
                or sum(item[0] == "proxy" and item[1] == "qfq" for item in self.calls) >= 3
            )
        )
        if self.behavior == "offline" or fail_primary:
            raise HttpTimeout()
        days = tuple(day for day in _DAYS if day <= end_day)
        days = days[-min(int(count), 640) :] if mode == "qfq" else days[-int(count) :]
        if (
            self.behavior == "resume_empty" and host == "direct" and older and not self.did_empty
            and not any(item[0] == "direct" and item[2] == _DAYS[-1] for item in self.calls)
        ):
            days = ()
            self.did_empty = True
        if self.behavior == "short_candidate" and int(count) < 2000:
            days = days[-1:]
        price = 11 if self.behavior == "resume_conflict" and host == "direct" else 10
        rows = [[day.isoformat(), price, price, price, price, 100, {}, "1", 20, "0", "0"] for day in days]
        stock = {"qfqday" if mode == "qfq" else "day": rows}
        payload = json.dumps({"code": 0, "data": {symbol: stock}})
        return HttpResponse(200, payload.encode())


def _supplier(
    wire: _Wire,
    *,
    options: TencentQfqOptions | None = None,
    clock: _Clock | None = None,
    failures: list[str] | None = None,
) -> TencentQfqSupplier:
    clock = clock or _Clock()
    return TencentQfqSupplier(
        TencentQfqDependencies(
            lambda: RequestsHttpTransport(monotonic=clock, execute_request=wire),
            lambda _day: (_SECURITY,),
            lambda: False,
            monotonic=clock,
            failures=(lambda _context, exc: failures.append(str(exc))) if failures is not None else None,
        ),
        options or TencentQfqOptions(30.0, 0),
    )


def test_review_budget_starts_lazily_and_counts_unique_codes_and_physical_attempts() -> None:
    clock = _Clock()
    budget = TencentRequestBudget(monotonic=clock)
    clock.now = 1000.0
    assert budget.reserve_code("600001")
    assert budget.reserve_code("600001")
    assert budget.reviewed_codes == ("600001",)
    for index in range(2, 9):
        assert budget.reserve_code(f"60000{index}")
    assert not budget.reserve_code("600009")
    assert budget.exhausted_reason == "code_limit"
    assert budget.reserve_request(30.0) == 15.0
    clock.now += 14.0
    assert budget.reserve_request(30.0) == 1.0
    clock.now += 1.0
    with pytest.raises(PriceReviewBudgetExhausted) as exhausted:
        budget.check_deadline()
    assert exhausted.value.reason == "deadline"


def test_shared_review_budget_pause_excludes_normal_sync_without_refilling_any_limit() -> None:
    clock = _Clock()
    budget = TencentRequestBudget(monotonic=clock)
    assert budget.reserve_code("600001")
    assert budget.reserve_request(30.0) == 15.0
    clock.now += 10.0
    budget.pause()
    clock.now += 1000.0
    assert budget.reserve_code("600002")
    assert budget.reserve_request(30.0) == 5.0
    assert budget.reviewed_codes == ("600001", "600002")
    assert budget.request_count == 2
    clock.now += 5.0
    budget.pause()
    clock.now += 1000.0
    assert not budget.reserve_code("600003")
    with pytest.raises(PriceReviewBudgetExhausted) as exhausted:
        budget.reserve_request(30.0)
    assert exhausted.value.reason == "deadline"
    assert budget.request_count == 2


def test_deadline_check_starts_discovery_sql_budget_and_resumes_only_remaining_time() -> None:
    clock = _Clock()
    budget = TencentRequestBudget(monotonic=clock)
    clock.now = 1000.0
    budget.check_deadline()
    clock.now += 14.0
    budget.pause()
    clock.now += 1000.0
    budget.check_deadline()
    assert budget.reserve_request(30.0) == 1.0
    clock.now += 1.0
    with pytest.raises(PriceReviewBudgetExhausted) as exhausted:
        budget.check_deadline()
    assert exhausted.value.reason == "deadline"
    assert budget.request_count == 1
    assert budget.reviewed_codes == ()


def test_sixteen_attempts_include_retries_and_alternate_and_never_become_supplier_failure() -> None:
    wire = _Wire("offline")
    failures: list[str] = []
    supplier = _supplier(wire, options=TencentQfqOptions(30.0, 2), failures=failures)
    budget = TencentRequestBudget(monotonic=_Clock())
    # Each unsuccessful stock normally consumes all 3 retries on both hosts.
    for _ in range(2):
        with pytest.raises(HistorySupplierPendingError):
            supplier.fetch_window(_SECURITY, _DAYS[-1:], request_budget=budget)
    with pytest.raises(PriceReviewBudgetExhausted) as exhausted:
        supplier.fetch_window(_SECURITY, _DAYS[-1:], request_budget=budget)
    assert exhausted.value.reason == "request_limit"
    assert str(exhausted.value) == "price_review_budget_exhausted"
    assert len(wire.calls) == budget.request_count == 16
    assert len(failures) == 16
    assert all(call[-1] == 15.0 for call in wire.calls)


def test_deadline_after_get_is_independent_and_blocks_qfq_without_logging_supplier_failure() -> None:
    clock, wire, failures = _Clock(), _Wire(), []
    budget = TencentRequestBudget(monotonic=clock)

    def advance() -> None:
        clock.now = 15.0

    wire.advance = advance
    with pytest.raises(PriceReviewBudgetExhausted) as exhausted:
        _supplier(wire, clock=clock, failures=failures).fetch_window(
            _SECURITY, _DAYS[-1:], request_budget=budget
        )
    assert exhausted.value.reason == "deadline"
    assert budget.request_count == len(wire.calls) == 1
    assert not failures


@pytest.mark.parametrize("cancellation", [KeyboardInterrupt, SystemExit, asyncio.CancelledError, CancelledError, HttpCancelled])
def test_cancellation_keeps_its_identity_when_budget_expires_during_get(
    cancellation: type[BaseException],
) -> None:
    clock, wire, failures = _Clock(), _Wire(), []
    budget = TencentRequestBudget(monotonic=clock)

    def cancel() -> None:
        clock.now = 15.0
        raise cancellation()

    wire.advance = cancel
    with pytest.raises(cancellation):
        _supplier(wire, clock=clock, failures=failures).fetch_window(
            _SECURITY, _DAYS[-1:], request_budget=budget
        )
    assert budget.request_count == len(wire.calls) == 1
    assert not failures


def test_budget_applies_to_older_pages_and_success_at_exact_request_limit_is_accepted() -> None:
    wire = _Wire()
    supplier = _supplier(wire)
    budget = TencentRequestBudget(PriceReviewConfiguration(maximum_requests=5), monotonic=_Clock())
    window = supplier.fetch_window(
        _SECURITY, _DAYS, request_count=2000, allow_interior_padding=True, request_budget=budget
    )
    assert all(cell.status == "complete" for cell in window.cells)
    assert len(wire.calls) == budget.request_count == 5
    short = TencentRequestBudget(PriceReviewConfiguration(maximum_requests=3), monotonic=_Clock())
    with pytest.raises(PriceReviewBudgetExhausted):
        supplier.fetch_window(
            _SECURITY, _DAYS, request_count=2000, allow_interior_padding=True, request_budget=short
        )
    assert short.request_count == 3


def test_normal_download_has_no_additional_review_limit() -> None:
    wire = _Wire()
    supplier = _supplier(wire)
    for _ in range(9):
        supplier.fetch_window(_SECURITY, _DAYS[-1:])
    assert len(wire.calls) == 18
    assert all(call[-1] == 30.0 for call in wire.calls)


@pytest.mark.parametrize("behavior", ["resume", "resume_after_two", "resume_conflict", "resume_empty"])
def test_opt_in_prefix_resume_matches_whole_window_and_conflicts_restart_whole_window(behavior: str) -> None:
    baseline, candidate = _Wire(behavior), _Wire(behavior)
    expected = _supplier(baseline).fetch_window(
        _SECURITY, _DAYS, request_count=2000, allow_interior_padding=True
    )
    actual = _supplier(candidate, options=TencentQfqOptions(30.0, 0, reuse_page_prefix=True)).fetch_window(
        _SECURITY, _DAYS, request_count=2000, allow_interior_padding=True
    )
    assert actual == expected
    assert all(cell.status == "complete" for cell in actual.cells)
    if behavior in {"resume", "resume_after_two"}:
        assert len(candidate.calls) == len(baseline.calls) - (2 if behavior == "resume_after_two" else 1)
        assert not any(host == "direct" and end == _DAYS[-1] for host, _, end, _, _ in candidate.calls)
    else:
        assert any(host == "direct" and end == _DAYS[-1] for host, _, end, _, _ in candidate.calls)


@pytest.mark.parametrize("behavior", ["complete", "short_candidate"])
def test_opt_in_minimum_rows_keeps_anchor_and_retries_original_window_when_incomplete(behavior: str) -> None:
    baseline, candidate = _Wire(behavior), _Wire(behavior)
    dates = _DAYS[-3:]
    expected = _supplier(baseline).fetch_window(
        _SECURITY, dates, request_count=2000, allow_interior_padding=True
    )
    actual = _supplier(candidate, options=TencentQfqOptions(30.0, 0, minimize_request_rows=True)).fetch_window(
        _SECURITY, dates, request_count=2000, allow_interior_padding=True
    )
    assert actual == expected
    assert all(call[2] == dates[-1] for call in candidate.calls)
    assert candidate.calls[0][3] == (dates[-1] - dates[0]).days + 1
    assert len(candidate.calls) == (2 if behavior == "complete" else 4)


@pytest.mark.parametrize(
    "changes",
    [
        {"maximum_codes": 9},
        {"maximum_requests": 17},
        {"maximum_seconds": 15.1},
        {"maximum_seconds": float("nan")},
        {"maximum_seconds": True},
        {"interval_sessions": 0},
    ],
)
def test_review_configuration_rejects_exceeding_limits_and_nonfinite_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="price_review_configuration_invalid"):
        replace(PriceReviewConfiguration(), **changes)
