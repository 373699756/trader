"""Explicit historical-industry preparation remains separate from normal price downloads."""

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.history.history_supplier_router import Baseline, Prices, _prepared
from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.historical_industry import BuildHistoricalIndustryPreparation
from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.download.infra.history_industry_preparation import PrepareHistoricalIndustries
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_OBSERVED = datetime(2026, 11, 6, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
_DATES = (date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7))
_SECURITY = BaoStockSecurity("600001", "测试股份", "main", date(2020, 1, 1), None, "fixture")


def _evidence(
    *,
    code="600001",
    first=date(2026, 1, 1),
    end=date(2026, 2, 1),
    source=1,
    category="J66",
    name="货币金融服务",
):
    announcements = {
        1: "2e1b516684544cc7be7eab6f93f7dddc",
        2: "d9221cd5eb1448e5956f0e9d84ecba89",
    }
    return OfficialIndustryEvidence(
        code=code,
        source_url="https://sp.capco.org.cn:82/file/202609/gwfile/2026shangbangupiaodaima.pdf",
        document_sha256=f"{source:064x}",
        disclosed_on=date(2025, 12, 31),
        effective_from=first,
        effective_to=end,
        received_at=_OBSERVED,
        page=1,
        excerpt=f"{code} {category} {name}",
        native_category=category,
        native_name=name,
        announcement_url="https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/" + announcements[source] + ".js",
        announcement_sha256=f"{source + 100:064x}",
    )


def _index(root):
    return SQLiteIndustryQueryIndex(root / "industry.sqlite3", "cninfo_industry_queries", sqlite_policy=SqlitePolicyFixture())


def _build(evidence, *, universe=(_SECURITY,), calendar=_DATES):
    return BuildHistoricalIndustryPreparation(universe, calendar, _OBSERVED, evidence, frozenset())


def test_finished_official_interval_is_read_and_prepared_without_current_day_filter(tmp_path):
    index = _index(tmp_path)
    fact = _evidence(end=date(2026, 1, 8))
    index.import_evidence((fact,))
    assert index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED) == (fact,)
    prepared = PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: False)
    assert prepared.readiness.ready and prepared.readiness.source == "capco"
    assert prepared.intervals[0].effective_from == fact.effective_from
    assert prepared.intervals[0].effective_to == fact.effective_to
    assert prepared.intervals[0].industry == "capco:J66货币金融服务"


def test_current_change_date_candidates_alone_never_create_historical_intervals(tmp_path):
    index = _index(tmp_path)
    record = {
        "SECCODE": "600001", "SECNAME": "测试股份", "ORGNAME": "测试股份有限公司", "VARYDATE": "2024-09-30",
        "F001V": "008001", "F002V": "中国上市公司协会上市公司行业分类标准", "F003V": "J66",
        "F004V": "金融业", "F005V": None, "F006V": "货币金融服务", "F007V": None, "F008C": "1",
    }
    original = IndustrySourceDocument(
        "cninfo:p_stock2110:600001",
        date(2020, 1, 1),
        _OBSERVED.date(),
        _OBSERVED,
        json.dumps({"resultcode": 200, "count": 1, "records": [record]}, ensure_ascii=False).encode(),
    )
    index.save_source_documents((original,))
    prepared = PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: False)
    assert not prepared.intervals
    assert prepared.readiness.candidate_codes == 1 and prepared.readiness.point_in_time_codes == 0
    assert not prepared.readiness.ready
    assert index.read_source_document(original.request_key, _OBSERVED) == original


def test_coverage_uses_trading_dates_and_preserves_uncovered_trading_day():
    first = _evidence(first=date(2026, 1, 2), end=date(2026, 1, 3))
    second = _evidence(first=date(2026, 1, 5), end=date(2026, 1, 7), source=2)
    calendar = (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    assert _build((first, second), calendar=calendar).readiness.ready
    missing = _build((first, replace(second, effective_from=date(2026, 1, 6))), calendar=calendar)
    assert len(missing.intervals) == 2 and missing.readiness.point_in_time_codes == 0


def test_intervals_clip_listing_boundaries_and_merge_same_classification():
    security = replace(_SECURITY, listed_on=date(2026, 1, 6), delisted_on=date(2026, 1, 8))
    first = _evidence(end=date(2026, 1, 7))
    second = _evidence(first=date(2026, 1, 7), source=2)
    prepared = _build((second, first), universe=(security,))
    assert prepared.readiness.ready and len(prepared.intervals) == 1
    assert (prepared.intervals[0].effective_from, prepared.intervals[0].effective_to) == (
        security.listed_on, security.delisted_on,
    )


def test_conflicting_official_industries_reject_the_preparation():
    with pytest.raises(ValueError, match="historical_industry_interval_conflict"):
        _build((_evidence(), _evidence(source=2, category="K70", name="房地产业")))


def test_equal_classification_with_revised_official_bytes_changes_source_identity():
    original = _evidence()
    before = _build((original,))
    after = _build((replace(original, document_sha256="a" * 64),))
    assert before.intervals == after.intervals
    assert before.evidence_hashes == (original.identity,)
    assert before.data_version != after.data_version


def test_same_official_source_bound_updates_keep_one_projection_and_original_receipt(tmp_path):
    index = _index(tmp_path)
    original = IndustrySourceDocument(
        "capco:pdf:" + hashlib.sha256(b"source-url").hexdigest(),
        _DATES[0],
        _DATES[-1],
        _OBSERVED,
        b"official-original",
    )
    index.save_source_documents((original,))
    fact = _evidence()
    assert index.import_evidence((fact,)) == 1
    assert index.import_evidence((fact,)) == 0
    extended = replace(fact, effective_to=date(2026, 3, 1))
    assert index.import_evidence((extended,)) == 1
    assert index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED) == (extended,)
    assert index.read_source_document(original.request_key, _OBSERVED) == original
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM official_industry_evidence").require_one()[0] == 1


def test_previous_different_original_survives_repeated_revised_content_and_observation_cutoff(tmp_path):
    index = _index(tmp_path)
    key = "capco:announcement:" + "a" * 64
    original = IndustrySourceDocument(key, _DATES[0], _DATES[-1], _OBSERVED - timedelta(days=2), b"original")
    revised = replace(original, received_at=_OBSERVED - timedelta(days=1), content=b"revised")
    repeated = replace(revised, received_at=_OBSERVED)
    for document in (original, revised, repeated):
        index.save_source_documents((document,))
    assert index.read_source_document(key, _OBSERVED) == repeated
    assert index.read_previous_source_document(key, _OBSERVED, differing_from=repeated.content_hash) == original
    assert index.read_previous_source_document(key, _OBSERVED, differing_from=original.content_hash) == repeated
    assert index.read_previous_source_document(key, original.received_at, differing_from=original.content_hash) is None
    assert index.read_previous_source_document(key, original.received_at, differing_from=revised.content_hash) == original
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE industry_source_document SET receipt_hash=? WHERE request_key=? AND content_hash=?",
            (b"x" * 32, key, bytes.fromhex(original.content_hash)),
        )
    with pytest.raises(ValueError, match="industry_source_document_hash_conflict"):
        index.read_previous_source_document(key, _OBSERVED, differing_from=repeated.content_hash)


def test_reclassification_conflict_rolls_back_every_projection_in_the_batch(tmp_path):
    index = _index(tmp_path)
    original = _evidence()
    index.import_evidence((original,))
    conflict = _evidence(source=2, category="K70", name="房地产业")
    with pytest.raises(ValueError, match="industry_evidence_interval_conflict"):
        index.import_evidence((_evidence(code="600002"), conflict))
    assert index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED) == (original,)


def test_projection_payload_tampering_is_rejected_without_repair(tmp_path):
    index = _index(tmp_path)
    index.import_evidence((_evidence(),))
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE official_industry_evidence SET code='600002'")
    with pytest.raises(ValueError, match="industry_evidence_hash_conflict"):
        index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED)


def test_future_receipts_and_disjoint_intervals_do_not_supply_history(tmp_path):
    index = _index(tmp_path)
    future = replace(_evidence(), received_at=_OBSERVED + timedelta(days=1))
    outside = _evidence(first=date(2026, 2, 1), end=date(2026, 3, 1), source=2)
    index.import_evidence((future, outside))
    assert index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED) == ()
    assert not _build((future,)).intervals


def test_explicit_industry_preparation_does_not_enter_normal_price_context(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.import_evidence((_evidence(),))
    reference = _prepared((_SECURITY,), _DATES, checked=_OBSERVED)
    prepared = PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: False)
    assert prepared.intervals and prepared.readiness.ready
    router = HistorySupplierRouter(Baseline(_DATES), Prices(), reference)
    context = router.load_context(_DATES[-1], len(_DATES))
    assert not context.industry_intervals and context.industry_readiness is None

    def failed(*args, **kwargs):
        raise ValueError("industry_evidence_hash_conflict")

    monkeypatch.setattr(SQLiteIndustryQueryIndex, "read_evidence_window", failed)
    unchanged = router.load_context(_DATES[-1], len(_DATES))
    assert unchanged == context
    with pytest.raises(ValueError, match="industry_evidence_hash_conflict"):
        PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: False)


def test_router_retains_the_real_prepared_reference_identity_without_official_history_dependencies():
    reference = replace(_prepared((_SECURITY,), _DATES, checked=_OBSERVED), industry_version="current-industry")
    context = HistorySupplierRouter(Baseline(_DATES), Prices(), reference).load_context(_DATES[-1], len(_DATES))
    assert context.reference_id == reference.identity
    assert context.st_evidence == reference.st_evidence
    assert context.calendar.open_dates == _DATES and context.universe == (_SECURITY,)
    assert context.industry_intervals == () and context.industry_readiness is None
    assert all(name != "capco_industry" for name, _ in context.source_versions.dependency_versions)


def test_missing_input_read_does_not_create_files_and_cancellation_propagates(tmp_path):
    index = _index(tmp_path / "missing")
    prepared = PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: False)
    assert not prepared.intervals and not index.path.parent.exists()
    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, _OBSERVED, cancel_requested=lambda: True)
    router = HistorySupplierRouter(
        Baseline(_DATES), Prices(), _prepared((_SECURITY,), _DATES, checked=_OBSERVED),
        cancel_requested=lambda: True,
    )
    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        router.load_context(_DATES[-1], len(_DATES))
    assert not index.path.parent.exists()


def test_window_row_limit_rejects_instead_of_returning_a_partial_history(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.import_evidence((_evidence(), _evidence(code="600002")))
    monkeypatch.setattr("trader.download.infra.industry_query_index._MAX_HISTORICAL_EVIDENCE", 1)
    with pytest.raises(ValueError, match="industry_evidence_rows_limit"):
        index.read_evidence_window(_DATES[0], _DATES[-1], _OBSERVED)


@pytest.mark.parametrize("kind", ("pdf", "catalog", "parsed"))
def test_large_official_original_is_a_single_transaction_without_expanding_cninfo_batches(tmp_path, kind):
    index = _index(tmp_path)
    content = b"x" * (2 * 1024 * 1024 + 1)
    original = IndustrySourceDocument("capco:" + kind + ":" + "a" * 64, _DATES[0], _DATES[-1], _OBSERVED, content)
    index.save_source_documents((original,))
    assert index.read_source_document(original.request_key, _OBSERVED) == original
    other = replace(original, request_key="capco:announcement:" + "b" * 64, content=b"official-announcement")
    with pytest.raises(ValueError, match="industry_source_batch_size_limit"):
        index.save_source_documents((original, other))
    with pytest.raises(ValueError, match="industry_source_document_invalid"):
        replace(original, request_key="cninfo:p_stock2110:600001")
    with pytest.raises(ValueError, match="industry_source_document_invalid"):
        replace(original, request_key="capco:parsed:not-a-digest")
    assert index.read_source_document(other.request_key, _OBSERVED) is None


def test_independent_industry_evidence_does_not_republish_price_history(tmp_path):
    observed = datetime(2026, 1, 7, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    reference = _prepared((_SECURITY,), _DATES, checked=observed)
    root = tmp_path / "history"
    configuration = HistorySyncConfigurationFixture(
        root,
        sessions=len(_DATES),
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=tmp_path / "reference",
    )
    initial_prices = Prices()
    first = RunHistoryFixture(
        configuration,
        HistorySupplierRouter(Baseline(_DATES), initial_prices, reference),
        clock=lambda: observed,
    )
    assert first.published
    control = HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    before = control.load_published_state().snapshot
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    old_rows = reader.read_day(_DATES[-1], before)
    assert reader.reference_index(before).reference.industry_intervals == ()
    index = _index(tmp_path / "official")
    index.import_evidence((replace(
        _evidence(), received_at=datetime(2026, 1, 1, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
    ),))

    class NoPriceDownload(Prices):
        def fetch_window(self, *_args, **_kwargs):
            pytest.fail("an industry-only change must not download historical prices again")

    second = RunHistoryFixture(
        configuration,
        HistorySupplierRouter(Baseline(_DATES), NoPriceDownload(), reference),
        clock=lambda: observed,
    )
    assert second.state == "already_current" and second.industry_readiness is None
    after = control.load_published_state().snapshot
    assert after == before
    assert reader.read_day(_DATES[-1], before) == old_rows
    assert reader.reference_index(before).reference.industry_intervals == ()
    assert reader.reference_index(after).reference.industry_intervals == ()
    assert PrepareHistoricalIndustries(index, (_SECURITY,), _DATES, observed, cancel_requested=lambda: False).readiness.ready
