from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from threading import Barrier, Condition, Event
from typing import Any
from zoneinfo import ZoneInfo

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
)
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.research import FinancialReport, ResearchObservation
from trader.recommendation.infra.market_data.announcement_batch_sync import CninfoAnnouncementBatchSynchronizer
from trader.recommendation.infra.market_data.announcement_sync import (
    CNINFO_ANNOUNCEMENT_PREFIX,
    CNINFO_COMPONENT_PREFIX,
    CNINFO_CURSOR_PREFIX,
    CninfoAnnouncementHttpClient,
    CninfoAnnouncementIncrementalSync,
    CninfoAnnouncementQuery,
    CninfoSyncResult,
    ParseCninfoAnnouncementRows,
)
from trader.recommendation.infra.market_data.market_cache_identity import _serialize_research_observation
from trader.recommendation.infra.market_data.market_task_runner import MarketTaskRunner
from trader.recommendation.infra.market_data.research_observation_loader import ResearchLoader
from trader.recommendation.infra.persistence.data_plane import SQLiteDataPlane

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 7, 30, 14, 50, tzinfo=SHANGHAI)


def test_cninfo_http_client_resolves_exact_security_and_bounds_public_query_page() -> None:
    calls: list[tuple[str, Mapping[str, str]]] = []
    responses = iter(
        (
            _Response([{"code": "600001", "category": "A股", "orgId": "gssh0600001"}]),
            _Response({"announcements": (), "hasMore": False}),
            _Response({"announcements": (), "hasMore": False}),
        )
    )

    def post(url: str, **kwargs: Any) -> _Response:
        calls.append((url, kwargs["data"]))
        return next(responses)

    client = CninfoAnnouncementHttpClient(
        post=post,
        timeout_seconds=8.0,
        wall_clock=lambda: OBSERVED_AT,
        sleeper=lambda _seconds: None,
    )

    client.fetch_announcements(
        CninfoAnnouncementQuery(
            code="600001",
            page=1,
            page_size=100,
            observed_at=OBSERVED_AT,
            cursor_value="",
            deadline=OBSERVED_AT.replace(minute=51),
        )
    )
    client.fetch_announcements(
        CninfoAnnouncementQuery(
            code="600001",
            page=2,
            page_size=100,
            observed_at=OBSERVED_AT,
            cursor_value="2026-07-01T09:00:00+08:00|latest",
            deadline=OBSERVED_AT.replace(minute=51),
        )
    )

    assert len(calls) == 3
    assert calls[1][1]["stock"] == "600001,gssh0600001"
    assert calls[1][1]["column"] == "sse"
    assert calls[1][1]["pageSize"] == "30"
    assert calls[1][1]["seDate"] == "1990-01-01~2026-07-30"
    assert calls[2][1]["seDate"] == "2026-07-01~2026-07-30"


def test_cninfo_http_client_uses_current_bse_market_scope() -> None:
    calls: list[Mapping[str, str]] = []
    responses = iter(
        (
            _Response([{"code": "920002", "category": "A股", "orgId": "gfbj0873843"}]),
            _Response({"announcements": (), "hasMore": False}),
        )
    )

    def post(_url: str, **kwargs: Any) -> _Response:
        calls.append(kwargs["data"])
        return next(responses)

    client = CninfoAnnouncementHttpClient(
        post=post,
        timeout_seconds=8.0,
        wall_clock=lambda: OBSERVED_AT,
        sleeper=lambda _seconds: None,
    )
    client.fetch_announcements(
        CninfoAnnouncementQuery(
            code="920002",
            page=1,
            page_size=30,
            observed_at=OBSERVED_AT,
            cursor_value="",
            deadline=OBSERVED_AT.replace(minute=51),
        )
    )

    assert calls[1]["column"] == "third"
    assert calls[1]["plate"] == "bj"
    assert calls[1]["stock"] == "920002,gfbj0873843"


def test_cninfo_http_client_allows_bounded_parallel_requests_without_a_global_post_lock() -> None:
    announcement_barrier = Barrier(2)

    def post(url: str, **kwargs: Any) -> _Response:
        data = kwargs["data"]
        if url.endswith("/topSearch/query"):
            code = data["keyWord"]
            return _Response([{"code": code, "category": "A股", "orgId": f"org-{code}"}])
        announcement_barrier.wait(timeout=2.0)
        return _Response({"announcements": (), "hasMore": False})

    client = CninfoAnnouncementHttpClient(
        post=post,
        timeout_seconds=8.0,
        wall_clock=lambda: OBSERVED_AT,
        sleeper=lambda _seconds: None,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(
            pool.submit(
                client.fetch_announcements,
                CninfoAnnouncementQuery(
                    code=code,
                    page=1,
                    page_size=30,
                    observed_at=OBSERVED_AT,
                    cursor_value="",
                    deadline=OBSERVED_AT.replace(minute=51),
                ),
            )
            for code in ("600001", "000001")
        )
        assert all(future.result()["hasMore"] is False for future in futures)


def test_missing_announcement_id_uses_persistent_evidence_digest_once() -> None:
    rows = {
        "announcements": (
            {"title": "关于重大违法强制退市决定的公告" * 30, "published_at": "2026-07-29 18:30:00"},
            {"title": "关于同时间其他事项的公告", "published_at": "2026-07-29 18:30:00"},
        )
    }
    parsed, invalid = ParseCninfoAnnouncementRows("600001", rows, OBSERVED_AT)
    again, _ = ParseCninfoAnnouncementRows("600001", rows, OBSERVED_AT)
    assert invalid == 0
    assert len({item.announcement_id for item in parsed}) == 2
    assert parsed == again
    assert all(item.announcement_id.isascii() and len(item.announcement_id) <= 80 for item in parsed)


def test_cninfo_sync_persists_unique_announcements_components_and_cursor(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    client = _StaticCninfoClient(
        (
            {
                "announcements": (
                    {
                        "announcementId": "risk-1",
                        "announcementTitle": "关于收到中国证监会立案告知书的公告",
                        "announcementTime": "2026-07-29 18:30:00",
                    },
                    {
                        "announcementId": "risk-1",
                        "announcementTitle": "关于收到中国证监会立案告知书的公告",
                        "announcementTime": "2026-07-29 18:30:00",
                    },
                    {
                        "announcementId": "invalid-1",
                        "announcementTitle": "",
                        "announcementTime": "2026-07-29 18:30:00",
                    },
                ),
                "hasMore": False,
            },
        )
    )

    result = CninfoAnnouncementIncrementalSync(client, data_plane, page_size=100).sync_code("600001", OBSERVED_AT)

    assert result.pages_fetched == 1
    assert result.saved_announcements == 1
    assert result.duplicate_rows == 1
    assert result.invalid_rows == 1
    assert result.history_complete is False
    saved = data_plane.load_risk_evidence_recent("600001", f"{CNINFO_ANNOUNCEMENT_PREFIX}risk-1")
    assert saved is not None
    assert saved.payload["exchange_cross_check_status"] == "pending"
    component = data_plane.load_risk_evidence_recent("600001", f"{CNINFO_COMPONENT_PREFIX}penalty")
    assert component is not None
    assert component.payload["status"] == "known_risk"
    cursor = data_plane.load_source_cursor_recent(f"{CNINFO_CURSOR_PREFIX}600001")
    assert cursor is not None
    assert cursor.payload["duplicate_rows"] == 1
    assert cursor.payload["history_complete"] is False
    assert cursor.payload["issuer_history_complete"] is False


def test_cninfo_empty_increment_does_not_clear_existing_risk(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    first_syncer = CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(
            (
                {
                    "announcements": (
                        {
                            "announcementId": "risk-1",
                            "announcementTitle": "关于重大违法强制退市决定的公告",
                            "announcementTime": "2026-07-28 18:30:00",
                        },
                    ),
                    "hasMore": False,
                },
            )
        ),
        data_plane,
    )
    second_syncer = CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(({"announcements": (), "hasMore": False},)),
        data_plane,
    )

    first = first_syncer.sync_code("600002", OBSERVED_AT)
    second = second_syncer.sync_code("600002", OBSERVED_AT.replace(hour=15))

    assert first.saved_announcements == 1
    assert second.saved_announcements == 0
    assert data_plane.load_risk_evidence_recent("600002", f"{CNINFO_ANNOUNCEMENT_PREFIX}risk-1") is not None
    component = data_plane.load_risk_evidence_recent("600002", f"{CNINFO_COMPONENT_PREFIX}forced_delisting")
    assert component is not None
    assert component.payload["status"] == "known_risk"


def test_cninfo_sync_checkpoints_each_page_and_resumes_fixed_range(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    first_client = _TimeoutAfterFirstPageCninfoClient()
    first = CninfoAnnouncementIncrementalSync(first_client, data_plane).sync_code("600009", OBSERVED_AT)

    assert first.pages_fetched == 1
    assert first.pages_checkpointed == 1
    assert first.checkpoint_advanced is True
    assert first.history_complete is False
    checkpoint = data_plane.load_source_cursor_recent(f"{CNINFO_CURSOR_PREFIX}600009")
    assert checkpoint is not None
    assert checkpoint.payload["range_start"] == "1990-01-01"
    assert checkpoint.payload["range_end"] == "2026-07-30"
    assert checkpoint.payload["next_page"] == 2

    next_observed_at = OBSERVED_AT.replace(day=31)
    second_client = _ResumeCninfoClient()
    second = CninfoAnnouncementIncrementalSync(second_client, data_plane).sync_code("600009", next_observed_at)

    assert second.resumed is True
    assert second.pages_fetched == 2
    assert second.pages_checkpointed == 2
    assert second.history_complete is True
    assert [(query.page, query.range_start, query.range_end) for query in second_client.queries] == [
        (2, date(1990, 1, 1), date(2026, 7, 30)),
        (1, date(2026, 7, 29), date(2026, 7, 31)),
    ]
    assert data_plane.load_risk_evidence_recent("600009", f"{CNINFO_ANNOUNCEMENT_PREFIX}resume-risk") is not None
    assert data_plane.load_risk_evidence_recent("600009", f"{CNINFO_ANNOUNCEMENT_PREFIX}resume-old") is not None
    component = data_plane.load_risk_evidence_recent("600009", f"{CNINFO_COMPONENT_PREFIX}penalty")
    assert component is not None
    assert component.payload["status"] == "known_risk"
    completed = data_plane.load_source_cursor_recent(f"{CNINFO_CURSOR_PREFIX}600009")
    assert completed is not None
    assert completed.payload["issuer_history_complete"] is True


def test_cninfo_benign_increment_does_not_downgrade_historical_risk(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(
            (
                {
                    "announcements": (
                        {
                            "announcementId": "risk-old",
                            "announcementTitle": "关于重大违法强制退市决定的公告",
                            "announcementTime": "2026-07-28 18:30:00",
                        },
                    ),
                    "hasMore": False,
                },
            )
        ),
        data_plane,
    ).sync_code("600008", OBSERVED_AT)

    CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(
            (
                {
                    "announcements": (
                        {
                            "announcementId": "benign-new",
                            "announcementTitle": "关于召开股东大会的公告",
                            "announcementTime": "2026-07-30 14:30:00",
                        },
                    ),
                    "hasMore": False,
                },
            )
        ),
        data_plane,
    ).sync_code("600008", OBSERVED_AT.replace(hour=15))

    component = data_plane.load_risk_evidence_recent("600008", f"{CNINFO_COMPONENT_PREFIX}forced_delisting")
    assert component is not None
    assert component.payload["status"] == "known_risk"


def test_cninfo_component_write_failure_does_not_advance_complete_cursor(monkeypatch, tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    save_records = data_plane.save_risk_evidence_recent_records

    def fail_component(records: tuple[RiskEvidenceRecord, ...]) -> None:
        if any(record.evidence_id.startswith(CNINFO_COMPONENT_PREFIX) for record in records):
            raise DataPlaneUnavailableError("component unavailable")
        save_records(records)

    monkeypatch.setattr(data_plane, "save_risk_evidence_recent_records", fail_component)
    result = CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(
            (
                {
                    "announcements": (
                        {
                            "announcementId": "case-write",
                            "announcementTitle": "普通公告",
                            "announcementTime": "2026-07-29 18:30:00",
                        },
                    ),
                    "hasMore": False,
                },
            )
        ),
        data_plane,
    ).sync_code("600005", OBSERVED_AT)

    assert result.source_errors
    assert data_plane.load_source_cursor_recent(f"{CNINFO_CURSOR_PREFIX}600005") is None


def test_cninfo_batch_sync_skips_completed_codes_on_same_day(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    syncer = CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(({"announcements": (), "hasMore": False},)),
        data_plane,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        batch = CninfoAnnouncementBatchSynchronizer(syncer, data_plane, pool, wall_clock=lambda: OBSERVED_AT)
        assert batch.pending_codes(("600006", "600007"), OBSERVED_AT) == ("600006", "600007")
        progress: list[tuple[int, int, int, int, int, int]] = []

        report = batch.sync_report(
            ("600006", "600007"),
            OBSERVED_AT,
            deadline=OBSERVED_AT.replace(minute=51),
            progress=progress.append,
        )

    assert report.completed_codes == ("600006", "600007")
    assert report.attempted_codes == ("600006", "600007")
    assert report.deferred_codes == ()
    assert report.failed_codes == ()
    assert progress[-1] == (2, 2, 2, 2, 0, 2)
    assert batch.pending_codes(("600006", "600007"), OBSERVED_AT) == ()


def test_cninfo_batch_sync_pauses_remaining_source_after_rate_limit(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        batch = CninfoAnnouncementBatchSynchronizer(
            _RateLimitedCninfoSync(),
            data_plane,
            pool,
            wall_clock=lambda: OBSERVED_AT,
        )
        report = batch.sync_report(
            ("600006", "600007"),
            OBSERVED_AT,
            deadline=OBSERVED_AT.replace(minute=51),
        )

    assert report.failed_codes == ("600006", "600007")
    assert report.deferred_codes == ()


def test_cninfo_batch_reports_confirmed_pages_before_stock_completion(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    client = _StaticCninfoClient(
        (
            {"announcements": (), "hasMore": True},
            {"announcements": (), "hasMore": False},
        )
    )
    progress: list[tuple[int, int, int, int, int, int]] = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        batch = CninfoAnnouncementBatchSynchronizer(
            CninfoAnnouncementIncrementalSync(client, data_plane),
            data_plane,
            pool,
            wall_clock=lambda: OBSERVED_AT,
        )
        report = batch.sync_report(
            ("600008",), OBSERVED_AT, deadline=OBSERVED_AT.replace(minute=51), progress=progress.append
        )

    assert report.completed_codes == ("600008",)
    assert progress[0][0] == 0
    assert progress[0][3] >= 1
    assert progress[-1] == (1, 1, 2, 2, 0, 1)


def test_cninfo_batch_retains_checkpointed_pages_from_unfinished_worker(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("trader.recommendation.infra.market_data.announcement_batch_sync._RESULT_DRAIN_SECONDS", 0.0)

    class PausedClient:
        def __init__(self) -> None:
            self.release = Event()
            self.second_started = Event()

        def fetch_announcements(self, query: CninfoAnnouncementQuery):
            if query.page == 1:
                return {
                    "announcements": (
                        {
                            "announcementId": "saved-before-deadline",
                            "announcementTitle": "关于控股股东拟减持股份的预披露公告",
                            "announcementTime": "2026-07-29 18:30:00",
                        },
                    ),
                    "hasMore": True,
                }
            self.second_started.set()
            self.release.wait(timeout=2.0)
            return {"announcements": (), "hasMore": False}

    data_plane = SQLiteDataPlane(tmp_path)
    client = PausedClient()
    progress: list[tuple[int, int, int, int, int, int]] = []
    deadline = OBSERVED_AT.replace(minute=51)
    clock_calls = 0

    def wall_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            return OBSERVED_AT
        assert client.second_started.wait(timeout=2.0)
        return deadline

    with ThreadPoolExecutor(max_workers=1) as pool:
        batch = CninfoAnnouncementBatchSynchronizer(
            CninfoAnnouncementIncrementalSync(client, data_plane),
            data_plane,
            pool,
            wall_clock=wall_clock,
        )
        report = batch.sync_report(
            ("600009",),
            OBSERVED_AT,
            deadline=deadline,
            progress=progress.append,
        )
        client.release.set()

    assert report.deadline_reached is True
    assert report.deferred_codes == ("600009",)
    assert report.changed_codes == ("600009",)
    assert report.pages_checkpointed == 1
    assert report.saved_announcements == 1
    assert progress[-1][3:5] == (1, 1)


def test_cninfo_batch_sync_limits_inflight_codes_to_three(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    syncer = _BoundedCninfoSync()
    codes = tuple(f"60000{index}" for index in range(1, 7))
    with ThreadPoolExecutor(max_workers=5) as workers, ThreadPoolExecutor(max_workers=1) as coordinator:
        batch = CninfoAnnouncementBatchSynchronizer(syncer, data_plane, workers, wall_clock=lambda: OBSERVED_AT)
        future = coordinator.submit(
            batch.sync_report,
            codes,
            OBSERVED_AT,
            deadline=OBSERVED_AT.replace(minute=51),
        )
        assert syncer.wait_until_started(3)
        assert syncer.started == 3
        syncer.release.set()
        report = future.result(timeout=2.0)

    assert syncer.max_active == 3
    assert report.completed_codes == codes


def test_research_loader_recovers_cninfo_announcements_as_structured_risk(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(
            (
                {
                    "announcements": (
                        {
                            "announcementId": "case-1",
                            "announcementTitle": "关于控股股东拟减持股份的预披露公告",
                            "announcementTime": "2026-07-29 18:30:00",
                        },
                    ),
                    "hasMore": False,
                },
            )
        ),
        data_plane,
    ).sync_code("600003", OBSERVED_AT)
    loader = ResearchLoader(
        None,
        MarketTaskRunner(
            worker_pool=None,
            source_lanes=None,
            cache=None,
            source_contracts={"akshare": "akshare-test"},
            config_version="test",
            schema_version="test",
            wall_clock=lambda: OBSERVED_AT,
        ),
        data_plane=data_plane,
        workers=1,
        ttl_seconds=600,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        capacity=10,
        cache_dir=None,
        json_writer=None,
        monotonic=lambda: 100.0,
    )

    loader.recover_from_data_plane()
    cached = loader.cached(("600003",), include_structured=True)

    observation = cached["600003"]
    assert observation.announcements_available is True
    assert observation.data_version != "research:unassigned"
    assert observation.corporate_risk_history_complete is False
    assert observation.corporate_risk_evidence_version.startswith("cninfo-risk-evidence:")
    assert len(observation.corporate_risk_evidence_version) == len("cninfo-risk-evidence:600003:") + 64
    assert {fact.evidence_id for fact in observation.corporate_risk_facts} == {f"{CNINFO_ANNOUNCEMENT_PREFIX}case-1"}
    assert loader.status().announcements_covered_count == 1


def test_cninfo_recovery_preserves_existing_financial_news_pledge_and_unlock(tmp_path: Path) -> None:
    data_plane = SQLiteDataPlane(tmp_path)
    observation = ResearchObservation(
        financial=FinancialReport(
            report_date=date(2025, 12, 31),
            published_at=OBSERVED_AT.replace(month=4, day=30),
        ),
        financial_history=(
            FinancialReport(
                report_date=date(2025, 12, 31),
                published_at=OBSERVED_AT.replace(month=4, day=30),
            ),
        ),
        pledge_ratio_pct=1.0,
        unlock_ratio_pct=2.0,
        news_queried=True,
        evidence=(
            Evidence(
                evidence_id="news:1",
                evidence_type="news",
                title="公司新闻",
                source="test",
                published_at=OBSERVED_AT.replace(day=29),
                received_at=OBSERVED_AT,
                data_version="news:1",
            ),
        ),
    )
    data_plane.save_risk_evidence_recent(
        RiskEvidenceRecord(
            code="600004",
            observed_at=OBSERVED_AT,
            source="akshare",
            source_time=OBSERVED_AT,
            data_version="research:existing",
            evidence_id="risk-observation:structured",
            payload=_serialize_research_observation(observation),
        )
    )
    CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(
            (
                {
                    "announcements": (
                        {
                            "announcementId": "case-2",
                            "announcementTitle": "关于收到立案告知书的公告",
                            "announcementTime": "2026-07-29 18:30:00",
                        },
                    ),
                    "hasMore": False,
                },
            )
        ),
        data_plane,
    ).sync_code("600004", OBSERVED_AT)
    loader = ResearchLoader(
        None,
        MarketTaskRunner(
            worker_pool=None,
            source_lanes=None,
            cache=None,
            source_contracts={"akshare": "akshare-test"},
            config_version="test",
            schema_version="test",
            wall_clock=lambda: OBSERVED_AT,
        ),
        data_plane=data_plane,
        workers=1,
        ttl_seconds=600,
        circuit_breaker_failures=3,
        circuit_breaker_seconds=60,
        capacity=10,
        cache_dir=None,
        json_writer=None,
        monotonic=lambda: 100.0,
    )

    loader.recover_from_data_plane()
    recovered = loader.cached(("600004",), include_structured=True)["600004"]

    assert recovered.financial is not None
    assert recovered.pledge_ratio_pct == 1.0
    assert recovered.unlock_ratio_pct == 2.0
    assert recovered.news_queried is True
    assert any(item.evidence_type == "news" for item in recovered.evidence)
    assert {fact.evidence_id for fact in recovered.corporate_risk_facts} == {f"{CNINFO_ANNOUNCEMENT_PREFIX}case-2"}


class _StaticCninfoClient:
    def __init__(self, pages: tuple[Mapping[str, object], ...]) -> None:
        self._pages = pages
        self.calls: list[tuple[int, str]] = []

    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]:
        self.calls.append((query.page, query.cursor_value))
        return self._pages[query.page - 1]


class _TimeoutAfterFirstPageCninfoClient:
    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]:
        if query.page > 1:
            raise TimeoutError("fixture deadline")
        return {
            "announcements": (
                {
                    "announcementId": "resume-risk",
                    "announcementTitle": "关于收到中国证监会立案告知书的公告",
                    "announcementTime": "2026-07-29 18:30:00",
                },
            ),
            "hasMore": True,
        }


class _ResumeCninfoClient:
    def __init__(self) -> None:
        self.queries: list[CninfoAnnouncementQuery] = []

    def fetch_announcements(self, query: CninfoAnnouncementQuery) -> Mapping[str, object]:
        self.queries.append(query)
        if query.range_end == date(2026, 7, 30):
            return {
                "announcements": (
                    {
                        "announcementId": "resume-old",
                        "announcementTitle": "历史普通公告",
                        "announcementTime": "2026-07-28 18:30:00",
                    },
                ),
                "hasMore": False,
            }
        return {"announcements": (), "hasMore": False}


class _RateLimitedCninfoSync:
    def sync_code(self, code: str, observed_at: datetime, *, deadline: datetime | None = None, progress=None):
        del code, observed_at, deadline, progress
        raise RuntimeError("http_429")


class _BoundedCninfoSync:
    def __init__(self) -> None:
        self.condition = Condition()
        self.release = Event()
        self.started = 0
        self.active = 0
        self.max_active = 0

    def wait_until_started(self, count: int) -> bool:
        with self.condition:
            return self.condition.wait_for(lambda: self.started >= count, timeout=2.0)

    def sync_code(
        self, code: str, observed_at: datetime, *, deadline: datetime | None = None, progress=None
    ) -> CninfoSyncResult:
        del observed_at, deadline, progress
        with self.condition:
            self.started += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.condition.notify_all()
        assert self.release.wait(timeout=2.0)
        with self.condition:
            self.active -= 1
        return CninfoSyncResult(code, 1, 0, 0, 0, True, "empty:2026-07-30", pages_checkpointed=1)


class _Response:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload
