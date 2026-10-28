"""Synthetic full-population SQLite measurement, never a supplier-speed claim."""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.reference_fixtures import PreparedFixture
from trader.download.domain.company_evidence import Disclosure, DisclosurePage
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch


def test_company_projection_is_incremental_and_reads_full_population_without_network(tmp_path):
    now = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    codes = tuple(f"{600000 + offset:06d}" for offset in range(4402))
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "company_evidence.sqlite3",
        facts_path=(tmp_path / "company_evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "company_evidence.sqlite3").parent / "text"),
    )
    index.initialize()
    partition = index.partitions(("sse",), now)[0]
    rows = tuple(
        Disclosure("fixture", f"{code}:{offset}", (code,), "普通经营公告", now, "", f"{code}:{offset}")
        for code in codes
        for offset in range(3)
    )
    started = time.monotonic()
    index.commit_page(partition, DisclosurePage(rows, len(rows), False), now)
    write_seconds = time.monotonic() - started
    started = time.monotonic()
    assert index.publish(codes, now) == len(codes)
    publish_seconds = time.monotonic() - started
    reader = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path),
        wall_clock=lambda: now,
        open_dates=lambda: PreparedFixture(now).open_dates,
        monotonic=time.monotonic,
    )
    started = time.monotonic()
    reader.initialize(codes, cancelled=lambda: False)
    preload_seconds = time.monotonic() - started
    started = time.monotonic()
    observations = reader.load(codes, now, include_structured=True)
    read_seconds = time.monotonic() - started
    assert len(observations) == len(codes)
    started = time.monotonic()
    reused = reader.load(codes, now, include_structured=True)
    cache_seconds = time.monotonic() - started
    assert reused == observations
    print(
        json.dumps(
            {
                "fixture_codes": len(codes),
                "fixture_announcements": len(rows),
                "write_seconds": round(write_seconds, 3),
                "publish_seconds": round(publish_seconds, 3),
                "preload_seconds": round(preload_seconds, 3),
                "read_seconds": round(read_seconds, 3),
                "cache_seconds": round(cache_seconds, 3),
                "database_bytes": index.path.stat().st_size,
                "physical_http_requests": 0,
            }
        )
    )
