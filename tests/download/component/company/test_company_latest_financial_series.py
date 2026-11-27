"""Frozen report order, exact missing issuers, pagination and scoped scoring observations."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.download.component.company.test_company_normalized_round import _baseline, _CUTOFF, _index, _observation, _page
from tests.download.component.company.test_company_sync_runtime import _runtime
from tests.download.unit.company.test_company_financial_source_records import _NOW, _payload, _row
from trader.download.application.company_financial_producer import BuildCompanyFinancialRoundQuery, ResolveCompanyFinancialScoringQuery
from trader.download.domain.company_evidence import CompanyInputValidationFailure, CompanyLatestFinancialQuery
from trader.download.domain.company_sync import CompanyFinancialRequestIdentity, CompanySyncBudget, CompanySyncFamily, CompanySyncPhasePlan
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.infra.company_structured_source import DecodeCompanyFinancialSourcePage
from trader.infra.http_contracts import HttpRequest


_Q3 = date(2026, 9, 30)
_H1 = date(2026, 6, 30)
_CODES = ("600001", "600002")


def _market(index, *, partial_target=False):
    query = ResolveCompanyFinancialScoringQuery(_CUTOFF)
    first = _observation(eps=None)
    prior = _observation(period=_H1, code="600002" if partial_target else "600001")
    page = replace(_page(first, prior), total=1005, pages=3, raw_count=500,
                   first_order_key=(_Q3, "000001.SZ"), last_order_key=(_H1, "600002.SH"))
    index.accept(query, page, _CODES, _CUTOFF)
    cursor = index.cursor(query, _CODES, _CUTOFF)
    assert isinstance(cursor, CompanyLatestFinancialQuery) and cursor.securities == _CODES
    assert index.financial_observation_watermark(query, _CODES, _CUTOFF) is None
    progress = index.financial_batch(query, _CODES, _CUTOFF)
    assert progress.accepted_pages == 1 and progress.expected_pages is None
    assert progress.market_prefix.read_pages == 1 and progress.market_prefix.declared_pages == 3
    assert progress.market_prefix.exact_issuer_count == 1
    return query, cursor, first, prior


def test_exact_group_requires_witness_and_keeps_new_report_null_when_older_value_exists(tmp_path):
    index, _ = _index(tmp_path, _baseline(tmp_path / "baseline"))
    query, exact, first, prior = _market(index)
    target = _observation(code="600002", period=_H1)
    with pytest.raises(CompanyInputValidationFailure, match="company_financial_exact_witness_missing"):
        index.accept(exact, _page(target), _CODES, _CUTOFF)
    assert index.cursor(query, _CODES, _CUTOFF) == exact
    with pytest.raises(CompanyInputValidationFailure, match="company_financial_exact_version_changed"):
        index.accept(exact, _page(_observation(eps=99), prior, target), _CODES, _CUTOFF)
    index.accept(exact, _page(first, prior, target), _CODES, _CUTOFF)
    assert index.cursor(query, _CODES, _CUTOFF) is None
    assert index.latest_financial_observations(query, _CODES, _CUTOFF) == (first, target)
    progress = index.financial_batch(query, _CODES, _CUTOFF)
    assert progress.pages_complete and progress.expected_pages == 2 and progress.accepted_raw_rows == 503
    assert progress.market_prefix.read_rows == 500 and progress.market_prefix.declared_rows == 1005
    assert dict(progress.missing_fields)["basic_eps"] == 1


def test_duplicate_disappearing_version_and_late_new_key_cannot_be_signed(tmp_path):
    index, _ = _index(tmp_path, _baseline(tmp_path / "baseline"))
    query, exact, first, prior = _market(index, partial_target=True)
    with pytest.raises(CompanyInputValidationFailure, match="company_financial_exact_witness_missing"):
        index.accept(exact, _page(first), _CODES, _CUTOFF)
    with pytest.raises(CompanyInputValidationFailure, match="company_financial_exact_version_changed"):
        index.accept(exact, _page(first, _observation(code="600002"), prior), _CODES, _CUTOFF)
    assert not index.latest_financial_observations(query, _CODES, _CUTOFF)


def test_fully_witnessed_missing_issuer_stays_unknown(tmp_path):
    index, _ = _index(tmp_path, _baseline(tmp_path / "baseline"))
    query, exact, first, prior = _market(index)
    index.accept(exact, _page(first, prior), _CODES, _CUTOFF)
    assert index.latest_financial_observations(query, _CODES, _CUTOFF) == (first,)
    assert index.financial_batch(query, _CODES, _CUTOFF).latest_observed_stocks == 1


def test_new_quarter_without_reports_scans_visible_period_and_never_cuts_off_an_open_period(tmp_path):
    index, _ = _index(tmp_path, _baseline(tmp_path / "baseline"))
    query = ResolveCompanyFinancialScoringQuery(_CUTOFF)
    observed = _observation(period=_H1)
    first = replace(_page(observed), pages=3, total=1001, raw_count=500,
                    first_order_key=(_H1, "000001.SZ"), last_order_key=(_H1, "600099.SH"))
    index.accept(query, first, _CODES, _CUTOFF)
    second = index.cursor(query, _CODES, _CUTOFF)
    assert second.page == 2 and not second.securities
    with pytest.raises(CompanyInputValidationFailure, match="company_financial_group_order_changed"):
        index.accept(second, replace(first, observations=(), page=2, first_order_key=first.last_order_key),
                     _CODES, _CUTOFF)
    assert index.cursor(query, _CODES, _CUTOFF) == second
    next_scope = ResolveCompanyFinancialScoringQuery(_CUTOFF + timedelta(days=1))
    assert index.cursor(next_scope, _CODES, _CUTOFF + timedelta(days=1)).page == 1


def test_resolve_previous_request_identity_and_keep_physical_unknown_distinct():
    full = BuildCompanyFinancialRoundQuery(_CUTOFF)
    plan = CompanySyncPhasePlan(CompanySyncFamily.FINANCIAL, CompanyFinancialRequestIdentity(full, "observations"))
    assert ResolveCompanyFinancialScoringQuery(_CUTOFF, plan) == full
    latest = ResolveCompanyFinancialScoringQuery(_CUTOFF)
    assert latest != full and CompanyFinancialRequestIdentity(latest, "observations") != plan.request_hash
    with pytest.raises(ValueError, match="company_financial_source_plan_unknown"):
        ResolveCompanyFinancialScoringQuery(_CUTOFF, replace(plan, request_hash="a" * 64))


def test_interrupted_old_window_keeps_unknown_network_attempt_and_is_not_reissued_as_new_market_query(tmp_path):
    runtime, _, transport, physical = _runtime(tmp_path)
    runtime.initialize(_CODES, _CUTOFF)
    old = BuildCompanyFinancialRoundQuery(_CUTOFF)
    def interrupt(query, *, observed_at):
        assert query == old and observed_at == _CUTOFF
        runtime.journal.authorize_request(HttpRequest("GET", "https://example.test/old-financial", 10))
        raise KeyboardInterrupt
    runtime._financial_source.financial_observations = interrupt
    budget = CompanySyncBudget(100, 20, 20, 10)
    with transport, pytest.raises(KeyboardInterrupt):
        runtime.run(_CODES, _CUTOFF, budget, cancel_requested=lambda: False, monotonic=lambda: 0, financial_query=old)
    previous = runtime.journal.unfinished_round()
    resumed, _, fresh_transport, fresh = _runtime(tmp_path)
    with fresh_transport:
        result = resumed.run(_CODES, _CUTOFF + timedelta(days=1), budget, cancel_requested=lambda: False,
                             monotonic=lambda: 0, new_refresh=False)
    assert result.round_id == previous.round_id and result.scope.cutoff == _CUTOFF
    assert resumed.journal.family(result.round_id, CompanySyncFamily.FINANCIAL).unknown_attempts == 1
    assert not physical and "https://example.test/financial" not in fresh


def test_repeated_version_keeps_first_observation_with_a_distinct_next_round_market_receipt(tmp_path):
    index, _ = _index(tmp_path, _baseline(tmp_path / "baseline"))
    observation = _observation()
    next_cutoff = _CUTOFF + timedelta(days=1)
    marks = []
    for when in (_CUTOFF, next_cutoff):
        query = ResolveCompanyFinancialScoringQuery(when)
        index.accept(query, _page(replace(observation, observed_at=when)), _CODES, when)
        assert index.latest_financial_observations(query, _CODES, when) == (observation,)
        marks.append(index.financial_observation_watermark(query, _CODES, when))
    assert all(marks) and marks[0] != marks[1]


def test_provider_requires_accepted_results_and_exact_code_and_report_order():
    query = ResolveCompanyFinancialScoringQuery(_NOW)
    def decode(payload, q=query):
        return DecodeCompanyFinancialSourcePage(payload, q, observed_at=_NOW, response_hash="a" * 64)
    with pytest.raises(DownloadValidationFailure, match="structured_source_not_accepted"):
        decode({"success":False, "code":9201, "result":None})
    row = _row()
    older = _row(REPORT_DATE="2026-06-30", REPORT_TYPE="中报")
    with pytest.raises(DownloadValidationFailure, match="financial_report_order_invalid"):
        decode(_payload(older, row))
    with pytest.raises(DownloadValidationFailure, match="financial_security_filter_ignored"):
        decode(_payload(row), replace(query, securities=("600001",)))
    valid = decode(_payload(row, older))
    assert valid.first_order_key[0] == _Q3 and valid.last_order_key[0] == _H1
    with pytest.raises(DownloadValidationFailure, match="financial_date_filter_ignored"):
        decode(_payload(_row(NOTICE_DATE="2026-11-17")))
    with pytest.raises(DownloadValidationFailure, match="company_financial_source_time_invalid"):
        decode(_payload(_row(UPDATE_DATE="2026-11-17")))
