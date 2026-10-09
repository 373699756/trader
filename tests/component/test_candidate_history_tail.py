from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.component.market_data_test_support import (
    FEATURE_WEIGHT_POLICY,
    LONG_POLICY,
    MARKET_REGIME_POLICY,
    NEWS_POLICY,
    TAIL_POLICY,
    FeatureBuilder,
    StaticGateway,
    StaticHistoryClient,
    _quote,
    _service,
)
from trader.download.application.fetch_history_tail import FetchHistoryTailUseCase
from trader.download.application.read_published_history import ReadPublishedHistoryUseCase
from trader.download.domain.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_tail import HistoryTailRequest, HistoryTailWindow
from trader.download.domain.published_history import PublishedHistoryManifest, PublishedHistoryWindow
from trader.infra.market_data.history.history import build_history_context
from trader.recommendation.domain.market.history_tail import HistoryQuality, plan_history_tail
from trader.recommendation.infra.market_data.history_tail_recovery import (
    CandidateHistoryTailRecovery,
    HistoryTailDependencies,
)
from trader.recommendation.infra.market_data.published_history_bars import qfq_bars
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

NOW = datetime(2026, 7, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
CALENDAR = tuple(
    day
    for offset in range(600)
    if (day := date(2025, 1, 1) + timedelta(days=offset)).weekday() < 5
    and day not in {date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5)}
)


def _cell(code: str, day: date) -> BaoStockDailyCell:
    price = 10.0 + (day - date(2025, 1, 1)).days / 1000
    raw = BaoStockDailySide(
        code,
        day,
        "unadjusted",
        price,
        price + 0.2,
        price - 0.2,
        price,
        100000.0,
        10000000.0 + price,
        price - 0.01,
        0.1,
        1.0,
        "trading",
    )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return BaoStockDailyCell(code, day, "complete", raw, qfq)


class _Archive:
    def __init__(self, sessions: int, gap: int, codes: tuple[str, ...]) -> None:
        dates = tuple(day for day in CALENDAR if day < NOW.date())
        cutoff = dates[-gap - 1] if gap else dates[-1]
        self.current = PublishedHistoryManifest(
            "a" * 64, 1, cutoff, tuple(day for day in CALENDAR if day <= cutoff), codes
        )
        self.sessions = sessions
        self.reads: list[tuple[str, ...]] = []

    def manifest(self):
        return self.current

    def iter_windows(self, manifest, *, sessions):
        for code in manifest.universe_codes:
            yield self.window(code, manifest.calendar_dates[-sessions:])

    def read_windows(self, manifest, codes, *, sessions):
        self.reads.append(tuple(codes))
        return tuple(self.window(code, manifest.calendar_dates[-sessions:]) for code in codes)

    @staticmethod
    def window(code: str, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        return PublishedHistoryWindow(
            code, tuple(HistoryRevision(1, "main", _cell(code, day), False, "行业", "fixture") for day in dates)
        )


class _Supplier:
    def __init__(self, *, conflict: str = "", kind: str = "complete", source: str = "baostock") -> None:
        self.calls: list[HistoryTailRequest] = []
        self.conflict = conflict
        self.kind = kind
        self.source = source
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def fetch(self, request: HistoryTailRequest, *, deadline: float) -> HistoryTailWindow:
        self.calls.append(request)
        if self.entered is not None and self.release is not None:
            self.entered.set()
            assert self.release.wait(2)
        cells = tuple(_cell(request.code, day) for day in request.dates)
        if request.code == self.conflict:
            first = cells[0]
            assert first.qfq is not None
            cells = (replace(first, qfq=replace(first.qfq, close_price=first.qfq.close_price - 0.01)), *cells[1:])
        if self.kind == "raw_only":
            cells = tuple(replace(cell, qfq=None, status="qfq_missing") for cell in cells)
        elif self.kind == "qfq_only":
            cells = tuple(replace(cell, unadjusted=None, status="unadjusted_missing") for cell in cells)
        elif self.kind == "unavailable":
            cells = tuple(replace(cell, unadjusted=None, qfq=None, status="unknown_missing") for cell in cells)
        elif self.kind == "incomplete":
            cells = cells[:-1]
        elif self.kind == "future":
            cells = (*cells[:-1], _cell(request.code, NOW.date() + timedelta(days=1)))
        return HistoryTailWindow(request.code, self.source, cells)


def _cache(sessions=61, gap=3, codes=("600001",), supplier=None, *, clock=None, cancelled=None, max_batch_size=120):
    archive = _Archive(sessions, gap, codes)
    supplier = supplier or _Supplier()
    clock = clock if clock is not None else [0.0]
    reader = ReadPublishedHistoryUseCase(archive)
    tail = CandidateHistoryTailRecovery(
        HistoryTailDependencies(
            reader,
            FetchHistoryTailUseCase(supplier),
            open_dates=lambda: CALENDAR,
            wall_clock=lambda: NOW,
            cancel_requested=cancelled or (lambda: False),
            monotonic=lambda: clock[0],
        ),
        max_batch_size=max_batch_size,
    )
    history = PublishedHistoryCache(reader, lookback_sessions=sessions, tail_recovery=tail)
    assert history.refresh()
    return history, archive, supplier, tail, clock


@pytest.mark.parametrize("sessions", (61, 251))
@pytest.mark.parametrize("gap", (1, 3, 5))
def test_weekly_history_tail_recomputes_the_full_profile_without_writing_archive(sessions, gap):
    history, archive, supplier, _, _ = _cache(sessions, gap)
    original_manifest = archive.current
    original_entries = history.entries()
    recall = history.load(("600001",), observed_at=NOW, recover_tail=False)
    assert supplier.calls == []
    assert len(recall["600001"]) == 20
    restrictions = {}
    loaded = history.load(("600001",), observed_at=NOW, action_restrictions=restrictions)
    assert restrictions == {}
    assert len(supplier.calls[0].dates) == gap + 3
    assert supplier.calls[0].dates[-1] < NOW.date()
    completed = tuple(day for day in CALENDAR if day < NOW.date())[-sessions:]
    expected = build_history_context(qfq_bars(archive.window("600001", completed)), lookback_sessions=sessions)
    assert history.summaries(loaded, NOW)["600001"] == expected
    assert history.summaries(loaded, NOW)["600001"] != original_entries["600001"].context
    assert archive.current == original_manifest
    assert history.entries() == original_entries
    assert (
        history.read_outcome_bars(("600001",), NOW)["600001"][-1].trade_date
        == original_manifest.data_cutoff.isoformat()
    )
    assert history.cached(("600001",), observed_at=NOW, fresh_only=True) == loaded
    history.load(("600001",), observed_at=NOW)
    assert len(supplier.calls) == 1


@pytest.mark.parametrize(
    ("kind", "source", "quality"),
    (
        ("complete", "tencent", HistoryQuality.ADJUSTMENT_CONFLICT),
        ("raw_only", "baostock", HistoryQuality.RAW_ONLY),
        ("qfq_only", "baostock", HistoryQuality.QFQ_ONLY),
        ("unavailable", "baostock", HistoryQuality.HISTORY_UNAVAILABLE),
        ("incomplete", "baostock", HistoryQuality.TAIL_PENDING),
        ("future", "baostock", HistoryQuality.TAIL_PENDING),
    ),
)
def test_tail_never_admits_wrong_source_adjustment_missing_or_future_rows(kind, source, quality):
    history, _, _, tail, _ = _cache(supplier=_Supplier(kind=kind, source=source))
    before = history.entries()
    restrictions = {}
    assert history.load(("600001",), observed_at=NOW, action_restrictions=restrictions) == {}
    assert restrictions == {"600001": {"history_data_pending"}}
    assert dict(tail.status().quality_counts)[quality] == 1
    assert history.entries() == before


def test_candidate_refresh_exposes_updated_features_and_isolates_one_conflicting_stock():
    codes = ("600001", "600002")
    history, _, supplier, _, _ = _cache(codes=codes, supplier=_Supplier(conflict="600002"))
    service = _service(
        StaticGateway(tuple(replace(_quote(code), source_time=NOW) for code in codes)),
        StaticHistoryClient(),
        FeatureBuilder(NEWS_POLICY, TAIL_POLICY, MARKET_REGIME_POLICY, LONG_POLICY, FEATURE_WEIGHT_POLICY),
        published_history=history,
        wall_clock=lambda: NOW,
    )
    features = service.refresh_candidate_quotes(codes, NOW)
    assert {feature.quote.code: feature.history_days for feature in features} == {"600001": 61, "600002": 0}
    assert "history_data_pending" in features[1].quote.execution_restrictions
    assert features[0].values["return_60d"] is not None
    read = service.read_candidate_features(codes, NOW)
    assert {feature.quote.code: feature.history_days for feature in read} == {"600001": 61, "600002": 0}
    assert len(supplier.calls) == 2
    health = service.health()
    assert health["history_tail_quality_counts"]["adjustment_conflict"] == 1
    assert health["history_tail_quality_counts"]["full_history_ready"] == 1
    assert health["history_tail_expected_date"] == "2026-07-15"
    assert "history_tail_codes" not in health


def test_tail_cache_identity_expires_by_time_session_and_snapshot():
    history, archive, supplier, _, clock = _cache()
    history.load(("600001",), observed_at=NOW)
    clock[0] = 901.0
    assert history.cached(("600001",), observed_at=NOW, fresh_only=True) == {}
    history.load(("600001",), observed_at=NOW)
    assert len(supplier.calls) == 2
    assert history.cached(("600001",), observed_at=NOW + timedelta(days=1), fresh_only=True) == {}
    archive.current = replace(archive.current, snapshot_hash="b" * 64)
    assert history.refresh()
    history.load(("600001",), observed_at=NOW)
    assert len(supplier.calls) == 3


@pytest.mark.parametrize("cancelled", (False, True))
def test_expired_deadline_or_cancellation_never_reads_archive_or_starts_supplier(cancelled):
    history, archive, supplier, _, _ = _cache(cancelled=lambda: cancelled)
    assert history.load(("600001",), observed_at=NOW, deadline=NOW if not cancelled else None) == {}
    assert archive.reads == []
    assert supplier.calls == []


def test_inflight_tail_is_not_duplicated_and_late_completion_is_not_admitted():
    supplier = _Supplier()
    supplier.entered, supplier.release = threading.Event(), threading.Event()
    history, _, _, _, clock = _cache(supplier=supplier)
    results = []
    thread = threading.Thread(target=lambda: results.append(history.load(("600001",), observed_at=NOW)))
    thread.start()
    try:
        assert supplier.entered.wait(2)
        assert history.load(("600001",), observed_at=NOW) == {}
        assert len(supplier.calls) == 1
        clock[0] = 20.0
    finally:
        supplier.release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert results == [{}]
    assert history.cached(("600001",), observed_at=NOW, fresh_only=True) == {}


def test_failed_tail_requests_rotate_before_retry_and_do_not_expand_the_window():
    codes = ("600001", "600002", "600003")
    history, _, supplier, _, clock = _cache(codes=codes, supplier=_Supplier(kind="incomplete"), max_batch_size=1)
    for index in range(3):
        history.load(codes, observed_at=NOW)
        clock[0] += 61.0
        assert len(supplier.calls) == index + 1
    assert tuple(request.code for request in supplier.calls) == codes
    assert all(len(request.dates) == 6 for request in supplier.calls)


def test_old_base_and_unverifiable_calendar_never_trigger_a_long_download():
    history, _, supplier, _, _ = _cache(gap=6)
    assert history.load(("600001",), observed_at=NOW) == {}
    assert supplier.calls == []
    assert dict(history.status().tail.quality_counts)[HistoryQuality.HISTORY_STALE] == 1
    with pytest.raises(ValueError, match="calendar_unverifiable"):
        plan_history_tail((date(2026, 7, 13),), date(2026, 7, 13), NOW.date())


def test_weekend_and_exchange_holiday_do_not_count_as_missing_sessions():
    friday = date(2026, 4, 30)
    holiday = date(2026, 5, 5)
    assert plan_history_tail(CALENDAR, friday, holiday).missing_dates == ()
    assert plan_history_tail(CALENDAR, date(2026, 7, 17), date(2026, 7, 19)).missing_dates == ()


def test_cached_candidate_reads_never_fetch_calendar_or_supplier(monkeypatch):
    history, _, supplier, tail, _ = _cache()
    loaded = history.load(("600001",), observed_at=NOW)

    def unexpected_calendar():
        pytest.fail("read-only feature assembly must not fetch the calendar")

    monkeypatch.setattr(tail, "_open_dates", unexpected_calendar)
    assert history.cached(("600001",), observed_at=NOW, fresh_only=True) == loaded
    assert history.summaries(loaded, NOW)["600001"].sample_count == 61
    assert len(supplier.calls) == 1


def test_incomplete_base_cannot_acquire_model_eligibility_from_a_valid_tail():
    history, archive, supplier, _, _ = _cache()
    original_read = archive.read_windows

    def incomplete_base(manifest, codes, *, sessions):
        return tuple(
            replace(window, revisions=window.revisions[4:])
            for window in original_read(manifest, codes, sessions=sessions)
        )

    archive.read_windows = incomplete_base
    assert history.load(("600001",), observed_at=NOW) == {}
    assert len(supplier.calls) == 1
    assert dict(history.status().tail.quality_counts)[HistoryQuality.HISTORY_UNAVAILABLE] == 1
