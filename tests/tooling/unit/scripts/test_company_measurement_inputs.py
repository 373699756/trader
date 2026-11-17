"""Synthetic measurement fixtures exercise the formal writer without the retired job."""

from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.infra.database.company_measurement_inputs import BuildCompanyMeasurementInputs
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.infra.sqlite_connection import SqliteSession


def test_synthetic_inputs_roundtrip_formal_facts_risk_receipts_and_empty_commit(tmp_path):
    at = datetime(2026, 10, 18, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    policy, events = SqlitePolicyFixture(), CompanyEventPolicyFixture()
    writer = SQLiteCompanyFactsIndex(tmp_path / "company.sqlite3", event_policy=events, sqlite_policy=policy)
    writer.initialize()
    codes = ("600001", "600002")
    inputs = BuildCompanyMeasurementInputs(codes, at, writer.extraction_hash, include_risks=True)
    assert len(inputs.facts) == 2 and len(inputs.receipts) == len(inputs.sources) == 6
    published = writer.publish(inputs.facts, dict(inputs.sources), None, at, receipts=inputs.receipts, field_results=())
    reader = SQLiteCompanyEvidenceReader(writer.path, event_policy=events, sqlite_policy=policy)
    assert reader.read(codes) == inputs.facts
    assert all(len(fact.risk_events) == 1 and len(fact.risk_events[0].evidence_ids) == 3 for fact in reader.read(codes))
    with SqliteSession(writer.path, sqlite_policy=policy) as connection:
        assert connection.run("SELECT COUNT(*) FROM company_input_receipt WHERE outcome='valid'").require_one()[0] == 6
    empty = writer.publish((), {}, published.identity, at, field_results=())
    assert empty.identity == published.identity and not empty.changed_codes
