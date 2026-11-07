from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.download.unit.history.test_historical_industry_preparation import _DATES, _OBSERVED, _SECURITY, _evidence
from tests.download.unit.history.test_history_industry_preparation import _Progress, _document
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.infra.history_industry_preparation import PrepareHistoricalIndustries
from trader.download.infra.industry_query_index import SQLiteIndustryQueryIndex
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.infra.sqlite_observation import SqliteObservation


def _index(tmp_path):
    return SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "cninfo_industry_queries", sqlite_policy=SqlitePolicyFixture())


def test_candidate_bulk_read_keeps_order_missing_and_time_cutoff_with_one_connection(tmp_path):
    index = _index(tmp_path)
    keys = tuple("cninfo:p_stock2110:" + f"{600001 + number:06d}" for number in range(130))
    originals = tuple(_document(key.rsplit(":", 1)[-1]) for key in (keys[0], keys[64], keys[-1]))
    for original in originals:
        index.save_source_documents((original,))
    index.save_source_documents((replace(originals[0], received_at=_OBSERVED + timedelta(days=1), content=b"future"),))
    observed = SqliteObservation()
    with observed.bind(), closing(index.iter_source_documents(keys, _OBSERVED, cancel_requested=lambda: False)) as rows:
        values = tuple(rows)
    assert tuple(item.request_key for item in values) == keys
    assert tuple(item.document for item in values if item.document is not None) == originals
    assert sum(item.document is None for item in values) == 127
    assert all(item.failure_category is None for item in values)
    assert observed.snapshot().connection_attempts == 1
    assert observed.snapshot().statements <= 8


def test_one_bad_candidate_receipt_does_not_hide_other_stock_originals(tmp_path):
    index = _index(tmp_path)
    originals = (_document("600001"), _document("600002"))
    index.save_source_documents(originals)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE industry_source_document SET payload=? WHERE request_key=?", (b"bad", originals[0].request_key))
    with closing(index.iter_source_documents(
        tuple(item.request_key for item in originals), _OBSERVED, cancel_requested=lambda: False,
    )) as rows:
        first, second = tuple(rows)
    assert first.document is None and first.failure_category == "industry_source_document_hash_conflict"
    assert second.document == originals[1] and second.failure_category is None


@pytest.mark.parametrize("corruption", ("digest", "payload"))
def test_bad_diagnostic_cninfo_preserves_independently_validated_official_intervals(tmp_path, corruption):
    index = _index(tmp_path)
    fact = _evidence()
    index.import_evidence((fact,))
    original = _document("600001")
    if corruption == "payload":
        original = replace(original, content=b'{"resultcode":200,"count":1,"records":[]}')
    index.save_source_documents((original,))
    if corruption == "digest":
        with SqliteSession(index.path) as connection, SqliteTransaction(connection):
            connection.run("UPDATE industry_source_document SET payload=?", (b"bad",))
    progress = _Progress()
    prepared = PrepareHistoricalIndustries(
        index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: False, progress=progress,
    )
    assert len(prepared.intervals) == prepared.readiness.point_in_time_codes == 1
    assert prepared.intervals[0].industry == fact.industry
    assert prepared.readiness.failure_category.startswith("cninfo_candidate_failures:1:600001:")
    assert progress.values[0].state == "failed" and progress.values[0].failure_reason == "cninfo_candidate_invalid"
    assert index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED) == (fact,)


def test_official_batch_validates_existing_rows_once_per_code_and_noop_skips_reencoding(tmp_path, monkeypatch):
    from trader.download.infra import industry_query_index

    index = _index(tmp_path)
    originals = tuple(_evidence(code=f"{600001 + number:06d}") for number in range(64))
    index.import_evidence(originals)
    incoming = tuple(replace(item, effective_to=_OBSERVED.date()) for item in originals)
    second = tuple(_evidence(code=item.code, first=_OBSERVED.date(), end=_OBSERVED.date() + timedelta(days=1), source=2)
                   for item in originals)
    decoded = []
    decode = SQLiteIndustryQueryIndex._evidence

    def observed_decode(self, row):
        decoded.append(row[1])
        return decode(self, row)

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "_evidence", observed_decode)
    assert index.import_evidence((*incoming, *second)) == 128
    assert len(decoded) == 64 and len(set(decoded)) == 64
    monkeypatch.setattr(industry_query_index, "IndustryEvidencePayload", lambda _item: pytest.fail("identical replay must not reencode a write payload"))
    assert index.import_evidence((*incoming, *second)) == 0
