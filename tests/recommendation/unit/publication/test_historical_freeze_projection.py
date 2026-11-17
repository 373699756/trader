"""Original baseline bytes remain historical evidence, without current adoption."""

import base64
import hashlib
import json
from datetime import date, timedelta
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers.recommendation.publication.freeze_records import record
from tests.helpers.recommendation.publication.freeze_identity import pipeline
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.sqlite_connection import SqliteConnectionOptions
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.web import CreateApp
from trader.http_api.route_services import UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_events import HistoricalFreezeObservation
from trader.recommendation.application.ports.freeze_records import FreezeRecordUnavailableError
from trader.recommendation.domain.publication.freeze_identity import CommittedFreezeRecord
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.publication.historical_pipeline import HistoricalPipelineAudit, HistoricalStageStatus
from trader.recommendation.infra.persistence.freeze_record_codec import CommittedRecordBytes, CommittedRecordFromBytes
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords
from trader.recommendation.infra.persistence.freeze_trace_index import SQLiteFreezeTraceIndex, _observation_from_bytes
from trader.recommendation.infra.persistence.historical_freeze_codec import HistoricalRecordFromBytes
from trader.training.application.research_audit import CommittedResearchAudit, HistoricalResearchAudit, HasCompleteResearchEvidence

_FIXTURE = Path(__file__).parents[3] / "fixtures/recommendation/history_553e9cc0.json"


def _saved():
    return json.loads(_FIXTURE.read_text())


def test_original_writer_bytes_hashes_and_missing_fields_are_not_rebuilt():
    saved = _saved()
    original = base64.b64decode(saved["record_base64"])
    assert hashlib.sha256(original).hexdigest() == saved["record_sha256"]
    projection = HistoricalRecordFromBytes(original)
    raw = json.loads(original)
    assert "score_ledger" not in raw["freeze"]["items"][0]
    assert projection.record_bytes == original
    assert projection.freeze.version == raw["freeze_version"]
    assert projection.freeze.content_hash == raw["freeze_hash"]
    assert projection.payload_hash == raw["payload_hash"] and projection.version == raw["version"]
    assert projection.freeze.pipeline is None
    assert not isinstance(projection, CommittedFreezeRecord)
    with pytest.raises(ValueError):
        CommittedRecordFromBytes(original)
    with pytest.raises(ValueError, match="historical"):
        CommittedFreezeRecord(projection.freeze, projection.committed_at, projection.commit_kind)


def test_old_file_is_read_only_and_current_writer_refuses_before_mutation(tmp_path):
    saved = _saved()
    path = tmp_path / "history.sqlite3"
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection:
        for statement in saved["freeze_sql_dump"].splitlines():
            connection.run(statement)
    before = path.read_bytes()
    reader = SQLiteFreezeRecords(path, sqlite_policy=SqlitePolicyFixture())
    projection = reader.load_history(Strategy.TOMORROW, date(2026, 8, 11))
    assert projection is not None and projection.record_bytes == base64.b64decode(saved["record_base64"])
    assert reader.list_dates(Strategy.TOMORROW) == (date(2026, 8, 11),)
    with pytest.raises(FreezeRecordUnavailableError):
        reader.initialize()
    assert path.read_bytes() == before
    assert reader.load_history(Strategy.TOMORROW, date(2026, 8, 11)) == projection


@pytest.mark.parametrize("field", ["freeze_hash", "payload_hash", "freeze_version", "version"])
def test_original_identity_or_hash_corruption_is_rejected(field):
    raw = json.loads(base64.b64decode(_saved()["record_base64"]))
    raw[field] = "corrupt"
    with pytest.raises(ValueError):
        HistoricalRecordFromBytes(json.dumps(raw).encode())


def test_current_bytes_are_strict_and_history_cannot_be_committed(tmp_path):
    original = record()
    current = replace(original, freeze=replace(original.freeze, pipeline=pipeline()))
    payload = CommittedRecordBytes(current)
    assert CommittedRecordBytes(CommittedRecordFromBytes(payload)) == payload
    raw = json.loads(payload)
    assert "schema_version" not in raw and "schema_version" not in raw["freeze"] and "fusion_version" not in raw["freeze"]
    historical = HistoricalRecordFromBytes(payload)
    target = SQLiteFreezeRecords(tmp_path / "current.sqlite3", sqlite_policy=SqlitePolicyFixture())
    target.initialize()
    with pytest.raises(ValueError, match="history"):
        target.commit(historical)
    assert target.load(Strategy.TOMORROW, current.trade_date) is None
    target.commit(current)
    assert target.recover().quarantined == 0


def _old_stage_graph():
    stages = ("input_readiness", "dynamic_filter", "board_cross_section", "strategy_history", "model_input",
              "candidate_score", "board_limit", "candidate_refresh", "input_coverage", "evidence_score",
              "model_cost_gate", "local_score", "deepseek_review", "fusion", "action_gate", "concentration")
    return HistoricalPipelineAudit("concentration", tuple(
        HistoricalStageStatus(key, "completed", 1, 1, (), None, (), ()) for key in stages
    ))


def test_saved_old_stage_graph_cannot_be_attached_to_a_current_freeze():
    with pytest.raises(ValueError, match="current pipeline stage graph"):
        replace(record().freeze, pipeline=_old_stage_graph())


def test_resigning_an_old_stage_graph_does_not_make_it_a_current_payload():
    raw = json.loads(CommittedRecordBytes(record()))
    graph = _old_stage_graph()
    raw["freeze"]["pipeline"] = {
        "current_stage": graph.current_stage,
        "stages": [{"key": item.key, "state": item.state, "input_count": 1, "output_count": 1,
                    "metric_ranges": [], "threshold": None, "facets": [], "reason_counts": []} for item in graph.stages],
    }
    raw["freeze_hash"] = CanonicalDigest(raw["freeze"])
    raw["payload_hash"] = CanonicalDigest({key: value for key, value in raw.items() if key not in {"payload_hash", "version"}})
    with pytest.raises(ValueError, match="current pipeline stage graph"):
        CommittedRecordFromBytes(json.dumps(raw).encode())


@pytest.mark.parametrize("field", ["is_formal", "risk_details", "score_ledger"])
def test_current_payload_cannot_omit_saved_empty_fields_while_retaining_its_complete_hash(field):
    raw = json.loads(CommittedRecordBytes(record()))
    target = raw["freeze"] if field == "is_formal" else raw["freeze"]["items"][0]
    target.pop(field)
    with pytest.raises(ValueError, match="complete canonical structure"):
        CommittedRecordFromBytes(json.dumps(raw).encode())


def test_real_history_get_preserves_missing_statistics_as_unknown(tmp_path):
    saved = _saved()
    path = tmp_path / "history.sqlite3"
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection:
        for statement in saved["freeze_sql_dump"].splitlines():
            connection.run(statement)
    before = path.read_bytes()
    history = SQLiteFreezeRecords(path, sqlite_policy=SqlitePolicyFixture())
    historical = HistoricalRecordFromBytes(base64.b64decode(saved["record_base64"]))

    class Clock:
        def now(self):
            return historical.committed_at + timedelta(days=1)

    queries = UnifiedFreezeQueries(UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), history, Clock())
    app = CreateApp(services=UnifiedWebServices(queries, UnifiedFreezeEventStream(), lambda: {}))
    response = app.test_client().get("/api/freezes/tomorrow/history?date=2026-08-11")
    assert response.status_code == 200
    body = response.get_json()
    assert body["coverage"]["candidate_count"] is None
    assert body["coverage"]["evaluated_count"] is None
    assert body["coverage"]["rejected_count"] is None
    assert body["selection_diagnostics"] is None
    assert body["coverage"]["selected_count"] == 1
    assert body["items"][0]["rank"] == 1
    assert path.read_bytes() == before


def test_historical_row_identity_mismatch_is_rejected_without_quarantine(tmp_path):
    saved = _saved()
    path = tmp_path / "history.sqlite3"
    with SqliteSession(path, sqlite_policy=SqlitePolicyFixture()) as connection:
        for statement in saved["freeze_sql_dump"].splitlines():
            connection.run(statement)
        connection.run("UPDATE freeze_record SET freeze_id='mismatched_original_index'")
    before = path.read_bytes()
    reader = SQLiteFreezeRecords(path, sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(FreezeRecordUnavailableError, match="identity mismatch"):
        reader.load_history(Strategy.TOMORROW, date(2026, 8, 11))
    assert path.read_bytes() == before
    with SqliteSession(path, SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture()) as connection:
        row = connection.run("SELECT status,record_bytes FROM freeze_record").require_one()
        assert row[0] == "committed" and row[1] == base64.b64decode(saved["record_base64"])


def test_original_complete_research_evidence_is_verified_without_a_new_audit_hash():
    saved = _saved()
    payload = base64.b64decode(saved["event_base64"])
    historical = _observation_from_bytes(payload, saved["event_hash"])
    assert isinstance(historical, HistoricalFreezeObservation)
    assert isinstance(historical.research_audit, HistoricalResearchAudit)
    assert not isinstance(historical.research_audit, CommittedResearchAudit)
    assert historical.event_bytes == payload and historical.event_hash == saved["event_hash"]
    assert HasCompleteResearchEvidence(historical.research_audit)


def test_original_missing_research_population_stays_unrecorded_and_unwritable(tmp_path):
    saved = _saved()
    payload = base64.b64decode(saved["legacy_event_base64"])
    historical = _observation_from_bytes(payload, saved["legacy_event_hash"])
    audit = historical.research_audit
    assert isinstance(audit, HistoricalResearchAudit)
    assert audit.input_observed_at is None and audit.point_in_time_population is None
    assert audit.point_in_time_population_hash is None and not HasCompleteResearchEvidence(audit)
    with pytest.raises(ValueError, match="read-only"):
        SQLiteFreezeTraceIndex(tmp_path, sqlite_policy=SqlitePolicyFixture()).record(historical)
    assert not tuple(tmp_path.iterdir())
