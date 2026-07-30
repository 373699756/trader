from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.infra.market_data.cninfo import (
    CNINFO_ANNOUNCEMENT_PREFIX,
    CNINFO_COMPONENT_PREFIX,
    CNINFO_CURSOR_PREFIX,
    CninfoAnnouncementIncrementalSync,
)
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_research import ResearchLoader
from trader.infra.persistence.data_plane import DataPlaneRepository

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 7, 30, 14, 50, tzinfo=SHANGHAI)


def test_cninfo_sync_persists_unique_announcements_components_and_cursor(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
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

    result = CninfoAnnouncementIncrementalSync(client, repository, page_size=100).sync_code("600001", OBSERVED_AT)

    assert result.pages_fetched == 1
    assert result.saved_announcements == 1
    assert result.duplicate_rows == 1
    assert result.invalid_rows == 1
    assert result.history_complete is True
    saved = repository.load_risk_evidence_recent("600001", f"{CNINFO_ANNOUNCEMENT_PREFIX}risk-1")
    assert saved is not None
    assert saved.payload["exchange_cross_check_status"] == "pending"
    component = repository.load_risk_evidence_recent("600001", f"{CNINFO_COMPONENT_PREFIX}penalty")
    assert component is not None
    assert component.payload["status"] == "known_risk"
    cursor = repository.load_source_cursor_recent(f"{CNINFO_CURSOR_PREFIX}600001")
    assert cursor is not None
    assert cursor.payload["duplicate_rows"] == 1
    assert cursor.payload["history_complete"] is True


def test_cninfo_empty_increment_does_not_clear_existing_risk(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
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
        repository,
    )
    second_syncer = CninfoAnnouncementIncrementalSync(
        _StaticCninfoClient(({"announcements": (), "hasMore": False},)),
        repository,
    )

    first = first_syncer.sync_code("600002", OBSERVED_AT)
    second = second_syncer.sync_code("600002", OBSERVED_AT.replace(hour=15))

    assert first.saved_announcements == 1
    assert second.saved_announcements == 0
    assert repository.load_risk_evidence_recent("600002", f"{CNINFO_ANNOUNCEMENT_PREFIX}risk-1") is not None
    component = repository.load_risk_evidence_recent("600002", f"{CNINFO_COMPONENT_PREFIX}forced_delisting")
    assert component is not None
    assert component.payload["status"] == "known_risk"


def test_research_loader_recovers_cninfo_announcements_as_structured_risk(tmp_path: Path) -> None:
    repository = DataPlaneRepository(tmp_path)
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
        repository,
    ).sync_code("600003", OBSERVED_AT)
    loader = ResearchLoader(
        None,
        MarketTaskRunner(
            worker_pool=None,
            source_lanes=None,
            cache=None,
            source_contract_versions={"akshare": "akshare-test"},
            config_version="test",
            schema_version="test",
            wall_clock=lambda: OBSERVED_AT,
        ),
        data_plane=repository,
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
    assert observation.corporate_risk_history_complete is True
    assert observation.corporate_risk_registry_version.startswith("cninfo-risk-registry:")
    assert {fact.evidence_id for fact in observation.corporate_risk_facts} == {f"{CNINFO_ANNOUNCEMENT_PREFIX}case-1"}
    assert loader.status().announcements_covered_count == 1


class _StaticCninfoClient:
    def __init__(self, pages: tuple[Mapping[str, object], ...]) -> None:
        self._pages = pages
        self.calls: list[tuple[int, str]] = []

    def fetch_announcements(
        self,
        code: str,
        *,
        page: int,
        page_size: int,
        observed_at: datetime,
        cursor_value: str,
    ) -> Mapping[str, object]:
        self.calls.append((page, cursor_value))
        del code, page_size, observed_at
        return self._pages[page - 1]
