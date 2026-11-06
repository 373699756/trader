"""Field evidence remains explicit, relational and atomic with its owning fact rows."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from trader.download.domain.company_events import CompanyRiskType, CompanySubject
from trader.download.domain.company_field_results import CompanyFieldConclusion, CompanyFieldEvidence, CompanyFieldSnapshot
from trader.download.infra.company_facts_protocol import EncodeCompanyDate, EncodeCompanyTime, RISK_TYPES
from trader.download.infra.company_facts_schema import COMPANY_FACTS_SCHEMA
from trader.download.infra.company_field_result_rows import ReadCompanyFieldResults, WriteCompanyFieldResults
from trader.infra.sqlite_connection import EnsureSqliteSchema, SqliteConnectionOptions, SqliteSession, SqliteTransaction


_AT = datetime(2026, 11, 6, 10, 15, 30, 123456, tzinfo=timezone.utc)


def _evidence(**changes):
    return replace(CompanyFieldEvidence(
        "cninfo", "announcement-one", "record-one", b"b" * 32, "implemented_total_capital_ratio:2",
        _AT, 3, 4, "本公司首次回购股份，占总股本0.10%。", "percent", CompanySubject.ISSUER,
    ), **changes)


def _conclusion(**changes):
    return replace(CompanyFieldConclusion(
        "600001", "event-one", "actual_pct", 0.1, "verified", True, "explicit_actual_measurement", (_evidence(),),
    ), **changes)


def _complete(results=(), code="600001"):
    defaults = (
        _conclusion(code=code, state="present_unverified", evidence=()),
        _conclusion(code=code, field="repurchase_purpose", value="cancellation", state="present_unverified", evidence=()),
        _conclusion(code=code, event_id="risk-one", field="start_date", value=_AT.date(), state="present_unverified", evidence=()),
        _conclusion(code=code, event_id="risk-one", field="end_date", value=None, state="verification_required", evidence=()),
        _conclusion(code=code, event_id="risk-one", field="planned_pct", value=None, state="verification_required", evidence=()),
        _conclusion(code=code, event_id="risk-one", field="completion_disclosed_at", value=_AT, state="present_unverified", evidence=()),
    )
    keyed = {(result.event_id, result.field): result for result in defaults}
    keyed.update({(result.event_id, result.field): result for result in results})
    return tuple(sorted(keyed.values(), key=lambda result: (result.code, result.event_id, result.field)))


@pytest.fixture
def connection(tmp_path):
    with SqliteSession(tmp_path / "fields.sqlite3", SqliteConnectionOptions(foreign_keys=True)) as connection:
        EnsureSqliteSchema(connection, COMPANY_FACTS_SCHEMA, allow_create=True)
        _seed(connection)
        yield connection


def _seed(connection, code="600001"):
    at = EncodeCompanyTime(_AT)
    connection.run(
        "INSERT INTO company_facts(code,finance_state,pledge_state,unlock_state,pending_docs,announcement_state,"
        "checked_at,published_at) VALUES(?,0,0,0,0,1,?,?)", (code, at, at),
    )
    connection.run(
        "INSERT INTO company_positive_event(code,event_key,event_type,subject_type,status,first_disclosed_at,"
        "last_disclosed_at,level,actual_pct,repurchase_purpose) VALUES(?,'event-one',1,1,2,?,?,0,0.1,1)",
        (code, at, at),
    )
    connection.run(
        "INSERT INTO company_risk_event(code,event_key,risk_type,subject_type,level,first_disclosed_at,"
        "last_disclosed_at,effective_at,status,resolved_at) VALUES(?,'risk-one',?,4,4,?,?,?,2,?)",
        (code, RISK_TYPES[CompanyRiskType.MAJOR_SHAREHOLDER_REDUCTION], at, at, at, at),
    )
    risk_id = connection.run("SELECT id FROM company_risk_event WHERE code=?", (code,)).require_one()[0]
    connection.run(
        "INSERT INTO company_reduction_plan(risk_id,start_date,completion_disclosed_at) VALUES(?,?,?)",
        (risk_id, EncodeCompanyDate(_AT.date()), at),
    )
    connection.run_many(
        "INSERT INTO company_evidence_ref(source,source_key) VALUES(1,?) ON CONFLICT(source,source_key) DO NOTHING",
        (("announcement-one",), ("risk-announcement",)),
    )
    connection.run(
        "INSERT INTO company_positive_evidence SELECT p.id,r.id,0 FROM company_positive_event p "
        "JOIN company_evidence_ref r ON r.source_key='announcement-one' WHERE p.code=?", (code,),
    )
    connection.run(
        "INSERT INTO company_risk_evidence SELECT p.id,r.id,0 FROM company_risk_event p "
        "JOIN company_evidence_ref r ON r.source_key='risk-announcement' WHERE p.code=?", (code,),
    )


@pytest.mark.parametrize("changes", (
    {"body_hash": b"short"}, {"body_hash": bytearray(32)}, {"source_key": ""}, {"record_hash": "bad\nrecord"},
    {"rule_id": ""}, {"published_at": _AT.replace(tzinfo=None)}, {"published_at": "2026-11-06"},
    {"start_line": True}, {"start_line": 0}, {"end_line": 2}, {"end_line": 4.0},
    {"quote": ""}, {"quote": " "}, {"quote": "q" * 1001}, {"quote": "bad\x00text"}, {"unit": "shares"},
    {"unit": []}, {"subject": "issuer"}, {"subject": None},
))
def test_evidence_rejects_unbounded_or_untyped_source_proof(changes):
    with pytest.raises(ValueError, match="company_field_evidence_"):
        _evidence(**changes)


@pytest.mark.parametrize("changes", (
    {"value": True}, {"value": 1}, {"value": float("nan")}, {"value": float("inf")}, {"value": object()},
    {"value": _AT.replace(tzinfo=None)}, {"value": ""}, {"value": "x" * 1001},
    {"code": "６００００１"}, {"code": "60001"}, {"event_id": ""}, {"field": "unselected"}, {"field": []},
    {"state": "complete"}, {"state": []}, {"reason": ""}, {"applicable": 1}, {"applicable": False},
    {"evidence": []}, {"evidence": (object(),)}, {"evidence": ()}, {"value": None},
    {"state": "present_unverified", "value": None},
    {"state": "not_disclosed"}, {"state": "not_disclosed", "value": None, "evidence": ()},
))
def test_conclusion_rejects_unproved_claims_and_invalid_typed_values(changes):
    with pytest.raises(ValueError, match="company_field_conclusion_"):
        _conclusion(**changes)


def test_field_records_are_immutable_and_never_infer_non_disclosure():
    evidence = _evidence()
    conclusion = _conclusion(state="present_unverified", evidence=())
    with pytest.raises(FrozenInstanceError):
        evidence.unit = "text"
    with pytest.raises(FrozenInstanceError):
        conclusion.state = "verified"
    assert conclusion.state == "present_unverified" and conclusion.evidence == ()
    explicit = _conclusion(state="not_disclosed", value=None)
    assert explicit.state == "not_disclosed" and explicit.evidence


def test_codec_roundtrips_values_from_fact_columns_and_explicit_states(connection):
    inputs = (
        _conclusion(),
        _conclusion(field="repurchase_purpose", value="cancellation", evidence=(_evidence(unit="text"),)),
        _conclusion(event_id="risk-one", field="start_date", value=_AT.date(),
                    evidence=(_evidence(unit="date", subject=CompanySubject.MAJOR_SHAREHOLDER),)),
        _conclusion(event_id="risk-one", field="completion_disclosed_at", value=_AT,
                    evidence=(_evidence(unit="time", subject=CompanySubject.MAJOR_SHAREHOLDER),)),
        _conclusion(event_id="risk-one", field="planned_pct", value=None, state="verification_required", evidence=()),
        _conclusion(event_id="risk-one", field="end_date", value=None, state="not_disclosed",
                    evidence=(_evidence(subject=CompanySubject.MAJOR_SHAREHOLDER),)),
    )
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", inputs)
    expected = tuple(sorted(inputs, key=lambda result: (result.code, result.event_id, result.field)))
    assert ReadCompanyFieldResults(connection, ("600001", "600001", "600002")) == expected
    assert ReadCompanyFieldResults(connection, ()) == ()
    assert ReadCompanyFieldResults(connection, ("600002",)) == ()
    columns = {row[1] for row in connection.run("PRAGMA table_info(company_field_result)")}
    assert columns == {"code", "event_key", "field", "state", "applicable", "reason"}
    assert "schema_version" not in {row[1] for row in connection.run("PRAGMA table_info(company_publication)")}


@pytest.mark.parametrize("changes", ({"value": 0.2}, {"event_id": "missing"}, {"field": "contract_amount"}))
def test_writer_rejects_stale_or_wrong_fact_binding_before_deleting_current_rows(connection, changes):
    original = _conclusion()
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((original,)))
    with pytest.raises(ValueError, match="company_field_result_value_mismatch"):
        WriteCompanyFieldResults(connection, "600001", _complete((_conclusion(**changes),)))
    assert ReadCompanyFieldResults(connection, ("600001",)) == _complete((original,))


def test_writer_rejects_duplicates_wrong_members_and_missing_fact(connection):
    result = _conclusion()
    with pytest.raises(ValueError, match="duplicate"):
        WriteCompanyFieldResults(connection, "600001", (result, result))
    with pytest.raises(ValueError, match="member_invalid"):
        WriteCompanyFieldResults(connection, "600002", (result,))
    with pytest.raises(ValueError, match="fact_missing"):
        WriteCompanyFieldResults(connection, "600002", ())
    with pytest.raises(ValueError, match="code_invalid"):
        ReadCompanyFieldResults(connection, ("６００００１",))


def test_codec_replacement_rollback_and_fact_cascade_are_atomic(connection):
    original = _conclusion()
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((original,)))
    replacement = _conclusion(state="present_unverified", evidence=())
    with pytest.raises(RuntimeError, match="interrupted"):
        with SqliteTransaction(connection, immediate=True):
            WriteCompanyFieldResults(connection, "600001", _complete((replacement,)))
            raise RuntimeError("interrupted")
    assert ReadCompanyFieldResults(connection, ("600001",)) == _complete((original,))
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((replacement,)))
    assert ReadCompanyFieldResults(connection, ("600001",)) == _complete((replacement,))
    assert connection.run("SELECT COUNT(*) FROM company_field_evidence").require_one()[0] == 0
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((original,)))
        connection.run("DELETE FROM company_facts WHERE code='600001'")
    assert connection.run("SELECT COUNT(*) FROM company_field_result").require_one()[0] == 0
    assert connection.run("SELECT COUNT(*) FROM company_field_evidence").require_one()[0] == 0


def test_read_order_is_stable_across_issuers_and_input_order(connection):
    _seed(connection, "600002")
    first = _conclusion()
    second = _conclusion(code="600002")
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600002", _complete((second,), "600002"))
        WriteCompanyFieldResults(connection, "600001", _complete((first,)))
    assert ReadCompanyFieldResults(connection, ("600002", "600001")) == (*_complete((first,)), *_complete((second,), "600002"))


def test_reader_rejects_incomplete_evidence_for_a_persisted_verified_claim(connection):
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((_conclusion(),)))
        connection.run("DELETE FROM company_field_evidence")
    with pytest.raises(ValueError, match="verified_proof_missing"):
        ReadCompanyFieldResults(connection, ("600001",))


def test_reader_rejects_noncontiguous_evidence_positions(connection):
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((_conclusion(),)))
        connection.run("UPDATE company_field_evidence SET position=1")
    with pytest.raises(ValueError, match="evidence_position_invalid"):
        ReadCompanyFieldResults(connection, ("600001",))


def test_reader_rejects_a_missing_published_field_conclusion(connection):
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete())
        connection.run("DELETE FROM company_field_result WHERE field='planned_pct'")
    with pytest.raises(ValueError, match="coverage_incomplete"):
        ReadCompanyFieldResults(connection, ("600001",))


@pytest.mark.parametrize("incomplete", ((), (_conclusion(),)))
def test_writer_requires_explicit_conclusions_for_all_current_fields(connection, incomplete):
    complete = _complete((_conclusion(),))
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", complete)
    with pytest.raises(ValueError, match="coverage_incomplete"):
        WriteCompanyFieldResults(connection, "600001", incomplete)
    assert ReadCompanyFieldResults(connection, ("600001",)) == complete


def test_no_event_issuer_may_have_an_empty_field_publication(connection):
    with SqliteTransaction(connection, immediate=True):
        connection.run("DELETE FROM company_positive_event")
        connection.run("DELETE FROM company_risk_event")
        WriteCompanyFieldResults(connection, "600001", ())
    assert ReadCompanyFieldResults(connection, ("600001",)) == ()


@pytest.mark.parametrize("identity,results", (
    ("", (_conclusion(),)), ("short", ()), ("g" * 64, ()), (None, ()),
    ("a" * 64, []), ("a" * 64, (object(),)), ("a" * 64, (_conclusion(), _conclusion())),
))
def test_snapshot_rejects_missing_identity_duplicate_or_mutable_results(identity, results):
    with pytest.raises(ValueError, match="company_field_snapshot_"):
        CompanyFieldSnapshot(identity, results)


def test_snapshot_keeps_empty_and_published_field_results_distinct():
    assert CompanyFieldSnapshot("", ()).results == ()
    snapshot = CompanyFieldSnapshot("a" * 64, (_conclusion(),))
    assert snapshot.publication_hash == "a" * 64 and snapshot.results == (_conclusion(),)
    with pytest.raises(FrozenInstanceError):
        snapshot.publication_hash = "b" * 64


def test_writer_rejects_equal_values_from_a_different_subject_before_replacing_current_results(connection):
    original = _complete((_conclusion(),))
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", original)
    wrong = _conclusion(evidence=(_evidence(subject=CompanySubject.CONTROLLING_SHAREHOLDER),))
    with pytest.raises(ValueError, match="subject_mismatch"):
        WriteCompanyFieldResults(connection, "600001", _complete((wrong,)))
    assert ReadCompanyFieldResults(connection, ("600001",)) == original


def test_reader_rejects_old_verified_proof_after_the_fact_subject_changes(connection):
    with SqliteTransaction(connection, immediate=True):
        WriteCompanyFieldResults(connection, "600001", _complete((_conclusion(),)))
        connection.run("UPDATE company_positive_event SET subject_type=2")
    with pytest.raises(ValueError, match="subject_mismatch"):
        ReadCompanyFieldResults(connection, ("600001",))
