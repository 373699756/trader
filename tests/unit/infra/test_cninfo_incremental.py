from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trader.recommendation.application.ports.data_plane_records import (
    DataPlaneUnavailableError,
    RiskEvidenceRecord,
)
from trader.recommendation.domain.market.models import Evidence
from trader.recommendation.domain.market.research import FinancialReport, ResearchObservation
from trader.recommendation.infra.market_data.announcement_sync import (
    CNINFO_ANNOUNCEMENT_PREFIX,
    CNINFO_COMPONENT_PREFIX,
    CNINFO_CURSOR_PREFIX,
    CninfoAnnouncementBatchSynchronizer,
    CninfoAnnouncementHttpClient,
    CninfoAnnouncementIncrementalSync,
    CninfoAnnouncementQuery,
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
        progress: list[tuple[int, int, int, int]] = []

        report = batch.sync_report(
            ("600006", "600007"),
            OBSERVED_AT,
            deadline=OBSERVED_AT.replace(minute=51),
            progress=lambda completed, total, pages, saved: progress.append((completed, total, pages, saved)),
        )

    assert report.completed_codes == ("600006", "600007")
    assert report.deferred_codes == ()
    assert report.failed_codes == ()
    assert progress[-1] == (2, 2, 2, 0)
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


class _RateLimitedCninfoSync:
    def sync_code(self, code: str, observed_at: datetime, *, deadline: datetime | None = None):
        del code, observed_at, deadline
        raise RuntimeError("http_429")


class _Response:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload
