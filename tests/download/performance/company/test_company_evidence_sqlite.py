"""Synthetic typed publication and preload measurement; no source/PDF/field-quality claim."""

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.infra.database.company_measurement_inputs import BuildCompanyMeasurementInputs
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture as PublishedCompanyResearch


def test_typed_company_publication_reads_full_population_without_network(tmp_path: Path) -> None:
    now = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    codes = tuple(f"{600000 + offset:06d}" for offset in range(4402))
    io, events = SqlitePolicyFixture(), CompanyEventPolicyFixture()
    index = SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3", event_policy=events, sqlite_policy=io)
    index.initialize()
    started = time.monotonic()
    inputs = BuildCompanyMeasurementInputs(codes, now, index.extraction_hash)
    typed_inputs_seconds = time.monotonic() - started
    started = time.monotonic()
    published = index.publish(inputs.facts, dict(inputs.sources), None, now, receipts=inputs.receipts, field_results=())
    assert len(published.changed_codes) == len(codes)
    publish_seconds = time.monotonic() - started
    reader = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.path, event_policy=events, sqlite_policy=io),
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
                "fixture_receipts": len(inputs.receipts),
                "company_measurement_scope": "synthetic_typed_inputs_and_formal_writer_only",
                "typed_inputs_seconds": round(typed_inputs_seconds, 3),
                "formal_publish_seconds": round(publish_seconds, 3),
                "preload_seconds": round(preload_seconds, 3),
                "read_seconds": round(read_seconds, 3),
                "cache_seconds": round(cache_seconds, 3),
                "database_bytes": index.path.stat().st_size,
                "physical_http_requests": 0,
            }
        )
    )
