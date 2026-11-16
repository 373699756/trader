"""Explicit metadata observations retain source identity without admitting a new main chain."""

import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.core.projections import BuildReport, NormalizeActionResult
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands
from scripts.download.company.company_structured_field_audit import AuditCompanyStructuredPage
from scripts.download.company import research_source_compare as worker
from tests.helpers.download.company.company_sources import Session
from trader.download.domain.company_evidence import StructuredCompanyQuery
from trader.download.domain.download_failure import DownloadValidationFailure
from trader.download.domain.company_source_records import CompanyFinancialMissingField, CompanyFinancialSourceRecordHash
from trader.download.infra.company_structured_source import (
    COMPANY_FINANCIAL_SOURCE_COLUMNS, DecodeCompanyFinancialSourcePage, EastmoneyCompanySource,
)

_NOW = datetime(2026, 11, 16, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_DAY = datetime(2026, 10, 27).date()
_QUERY = StructuredCompanyQuery("financial", _DAY, _DAY)


def _row(**changes):
    return {
        "SECUCODE": "000912.SZ", "SECURITY_CODE": "000912", "REPORT_DATE": "2026-09-30 00:00:00",
        "REPORT_TYPE": "三季报", "NOTICE_DATE": "2026-10-27 09:12:34", "UPDATE_DATE": "2026-10-28 10:11:12",
        "CURRENCY": "CNY", "EPSJB": 0, "BPS": None, "TOTALOPERATEREVETZ": -9.55,
        "PARENTNETPROFITTZ": -8001.93, "KCFJCXSYJLRTZ": -328.78, "ROEJQ": -2.32,
        "PARENTNETPROFIT": -145278087.49, "KCFJCXSYJLR": -167418348.35, **changes,
    }


def _payload(*rows):
    return {"code": 0, "success": True, "result": {"count": len(rows), "pages": 1, "data": list(rows)}}


def _decode(payload, *, observed_at=_NOW):
    return DecodeCompanyFinancialSourcePage(payload, _QUERY, observed_at=observed_at,
        response_hash=hashlib.sha256(json.dumps(payload).encode()).hexdigest())


def test_same_row_keeps_zero_negative_null_source_times_and_currency_units():
    page = _decode(_payload(_row()))
    item, = page.observations
    assert item.financial.basic_eps == 0 and item.financial.core_net_profit == -167418348.35
    assert item.missing_fields == (CompanyFinancialMissingField("book_value_per_share"),)
    assert item.business_key == ("000912", datetime(2026, 9, 30).date())
    assert item.source_notice_at.isoformat() == "2026-10-27T09:12:34+08:00"
    assert item.source_updated_at.isoformat() == "2026-10-28T10:11:12+08:00"
    assert item.financial.published_at.hour == 0 and item.currency == "CNY"
    assert dict(item.units)["basic_eps"] == "currency_per_share"
    assert dict(item.units)["roe_pct"] == "percentage_points"
    assert dict(item.units)["core_net_profit"] == "currency"
    assert _decode(_payload(_row()), observed_at=_NOW + timedelta(hours=1)).observations[0].record_hash == item.record_hash
    assert _decode(_payload(_row(UPDATE_DATE="2026-10-29"))).observations[0].record_hash != item.record_hash


def test_non_a_source_rows_remain_in_coverage_but_not_accepted_observations():
    page = _decode(_payload(_row(), _row(SECUCODE="833042.NQ", SECURITY_CODE="833042"),
                            _row(SECUCODE="A26111.SH", SECURITY_CODE="A26111")))
    assert page.raw_count == page.total == 3 and len(page.observations) == 1


@pytest.mark.parametrize("identity,code", ((None, "000912"), (123456, "123456"), ("833042.NQ", "833043")))
def test_malformed_raw_security_identity_is_not_reclassified_as_an_excluded_market(identity, code):
    with pytest.raises(ValueError, match="financial_source_identity_invalid"):
        _decode(_payload(_row(SECUCODE=identity, SECURITY_CODE=code)))


def test_old_report_with_recent_source_notice_is_preserved_without_original_disclosure_claim():
    item, = _decode(_payload(_row(REPORT_DATE="2025-09-30", UPDATE_DATE=None))).observations
    assert item.business_key[1].year == 2025 and item.source_notice_at.year == 2026
    assert item.source_updated_at is None


@pytest.mark.parametrize("field", COMPANY_FINANCIAL_SOURCE_COLUMNS)
def test_missing_metadata_or_value_column_is_not_silently_filled(field):
    row = _row()
    row.pop(field)
    with pytest.raises(ValueError):
        _decode(_payload(row))


@pytest.mark.parametrize("changes", (
    {"CURRENCY": "USD"}, {"NOTICE_DATE": "2026-10-26"}, {"NOTICE_DATE": "2026-10-27 garbage"},
    {"UPDATE_DATE": "2026-11-17"}, {"UPDATE_DATE": "bad-private-source-time"},
    {"REPORT_TYPE": "中报"}, {"REPORT_DATE": "2026-09-29"}, {"REPORT_DATE": "2026-12-31"},
    {"SECUCODE": "000912.SH"}, {"SECURITY_CODE": "000001"}, {"EPSJB": True},
))
def test_bad_identity_dates_units_or_numeric_values_are_rejected(changes):
    with pytest.raises(ValueError):
        _decode(_payload(_row(**changes)))


def test_duplicate_report_keys_and_forged_missing_reason_are_rejected():
    with pytest.raises(ValueError, match="company_financial_source_page_invalid"):
        _decode(_payload(_row(), _row(UPDATE_DATE="2026-10-29")))
    item, = _decode(_payload(_row())).observations
    with pytest.raises(ValueError, match="company_financial_source_observation_invalid"):
        replace(item, missing_fields=())


@pytest.mark.parametrize("total,pages,raw_count", ((0, 2, 0), (1, 2, 1), (2, 1, 1)))
def test_direct_page_construction_rejects_contradictory_counts(total, pages, raw_count):
    page = _decode(_payload(_row()))
    with pytest.raises(ValueError, match="company_financial_source_page_invalid"):
        replace(page, total=total, pages=pages, raw_count=raw_count,
                observations=() if raw_count == 0 else page.observations)


def test_direct_observation_rejects_later_same_day_financial_timestamp():
    item, = _decode(_payload(_row())).observations
    with pytest.raises(ValueError, match="company_financial_source_time_invalid"):
        replace(item, source_notice_at=_NOW.replace(hour=9),
                financial=replace(item.financial, published_at=_NOW + timedelta(hours=1)))


@pytest.mark.parametrize("problem", ("future_update", "duplicate_business_key", "report_after_notice"))
def test_supplier_domain_violations_remain_typed_source_validation_failures(problem):
    if problem == "future_update":
        payload = _payload(_row(UPDATE_DATE="2026-11-17"))
    elif problem == "duplicate_business_key":
        payload = _payload(_row(), _row(UPDATE_DATE="2026-10-29"))
    else:
        payload = _payload(_row(REPORT_DATE="2026-12-31", REPORT_TYPE="年报"))
    with pytest.raises(DownloadValidationFailure) as captured:
        _decode(payload)
    assert isinstance(captured.value.__cause__, ValueError)


@pytest.mark.parametrize("problem", ("query", "response_hash"))
def test_local_decoder_parameters_are_not_reclassified_as_supplier_failures(problem):
    with pytest.raises(ValueError) as captured:
        DecodeCompanyFinancialSourcePage(_payload(_row()), replace(_QUERY, component="pledge") if problem == "query" else _QUERY,
            observed_at=_NOW, response_hash="0" * 64 if problem == "query" else "bad-local-hash")
    assert not isinstance(captured.value, DownloadValidationFailure)


@pytest.mark.parametrize("change", ("hash", "financial", "source_time"))
def test_same_row_content_hash_cannot_be_reused_for_different_normalized_content(change):
    item, = _decode(_payload(_row())).observations
    with pytest.raises(ValueError, match="company_financial_source_hash_mismatch"):
        if change == "hash":
            replace(item, record_hash="0" * 64)
        elif change == "financial":
            replace(item, financial=replace(item.financial, basic_eps=1))
        else:
            replace(item, source_updated_at=item.source_updated_at + timedelta(days=1))
    financial = replace(item.financial, basic_eps=1)
    digest = CompanyFinancialSourceRecordHash(
        financial, item.source_security_identity, item.source_notice_at, item.source_updated_at, item.currency,
    )
    assert replace(item, financial=financial, record_hash=digest).record_hash == digest


@pytest.mark.parametrize("number", (1, 1.0, 0, 0.0, -0.0))
def test_numeric_encoding_changes_cannot_change_normalized_record_identity(number):
    item, = _decode(_payload(_row(EPSJB=number))).observations
    same = replace(item.financial, basic_eps=number)
    assert replace(item, financial=same).record_hash == item.record_hash
    equivalent = replace(same, basic_eps=float(number))
    assert CompanyFinancialSourceRecordHash(
        equivalent, item.source_security_identity, item.source_notice_at, item.source_updated_at, item.currency,
    ) == item.record_hash
    if number == 0:
        assert _decode(_payload(_row(EPSJB=0.0))).observations[0].record_hash == item.record_hash


def test_explicit_adapter_uses_fifteen_columns_and_preserves_default_fetch():
    session = Session(_payload(_row()))
    adapter = EastmoneyCompanySource(session, monotonic=lambda: 0, timeout_seconds=8)
    observation = adapter.financial_observations(_QUERY, observed_at=_NOW)
    assert observation.raw_count == 1 and len(session.requests) == 1
    assert observation.response_hash == hashlib.sha256(json.dumps(session.payload).encode()).hexdigest()
    assert session.requests[0]["max_response_bytes"] == 1024 * 1024
    assert session.requests[0]["params"]["sty"].split(",") == list(COMPANY_FINANCIAL_SOURCE_COLUMNS)
    adapter.fetch(_QUERY, observed_at=_NOW)
    assert "UPDATE_DATE" not in session.requests[1]["params"]["sty"]
    assert session.requests[1]["max_response_bytes"] == 16 * 1024 * 1024


def test_source_audit_and_public_projection_consume_real_typed_observations():
    report = AuditCompanyStructuredPage(Session(_payload(_row())), _QUERY, observed_at=_NOW,
        timeout_seconds=8, deadline=60, monotonic=lambda: 0, financial_source_records=True)
    assert report["status"] == "decoded" and report["source_records"]["source_null_fields"] == 1
    result = NormalizeActionResult("research-source-compare", DiagnosticResult("research_source_compare", 0, 0, report, None))
    assert result.payload["status"] == "degraded"
    assert NormalizeActionResult("research-source-compare", result) == result
    public = BuildReport("research-source-compare", (result,))["checks"][0]["summary"]
    assert public["source_records"]["accepted_rows"] == 1
    assert public["source_records"]["issuer_revision_verified"] is False
    assert public["requested_fields"] == list(COMPANY_FINANCIAL_SOURCE_COLUMNS)
    assert "identity_samples" not in public["source_records"]
    assert not public["production_adopted"] and not public["full_window_coverage_verified"]
    report["source_records"]["issuer_revision_verified"] = True
    failed = NormalizeActionResult("research-source-compare", DiagnosticResult("research_source_compare", 0, 0, report, None))
    assert failed.payload["status"] == "failed"


def test_cli_selection_is_explicit_and_reaches_the_worker_command():
    options = ParseOptions(BuildParser("research-source-compare").parse_args([
        "--structured-page", "financial", "--structured-start", "2026-10-27", "--structured-end", "2026-10-27",
        "--financial-source-records",
    ]))
    command, = BuildCommands(options)
    assert "--financial-source-records" in command.argv
    default = replace(options, financial_source_records=False)
    assert "--financial-source-records" not in BuildCommands(default)[0].argv
    for bad in (replace(options, structured_page="pledge"), replace(options, codes=("000912",)),
                replace(options, structured_page=None)):
        with pytest.raises(ValueError):
            BuildCommands(bad)


def test_worker_main_executes_the_selected_observation_adapter(tmp_path, monkeypatch, capsys):
    config = tmp_path / "runtime.json"
    config.write_text("{}", encoding="utf-8")
    session = Session(_payload(_row(PRIVATE_SOURCE_FIELD="private-source-value")))
    monkeypatch.setattr(worker, "LoadRuntimeSettings", lambda _path: object())
    monkeypatch.setattr(worker, "OpenExternalHttp", lambda **_kwargs: nullcontext(session))
    monkeypatch.setattr("sys.argv", ["sources", "--config", str(config), "--structured-page", "financial",
        "--structured-start", "2026-10-27", "--structured-end", "2026-10-27", "--financial-source-records"])
    assert worker.Main() == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["financial_source_records_requested"] is True and report["source_records"]["accepted_rows"] == 1
    assert len(session.requests) == 1 and session.requests[0]["max_response_bytes"] == 1024 * 1024
    assert "private-source-value" not in output and report["production_adopted"] is False


@pytest.mark.parametrize("arguments", (
    ["--financial-source-records"], ["--financial-source-records", "--structured-page", "pledge"],
    ["--financial-source-records", "--structured-page", "financial", "--codes", "000912"],
))
def test_invalid_metadata_selection_is_rejected_before_configuration_or_http(monkeypatch, arguments):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid observation must not reach I/O")

    monkeypatch.setattr(worker, "LoadRuntimeSettings", forbidden)
    monkeypatch.setattr(worker, "OpenExternalHttp", forbidden)
    monkeypatch.setattr("sys.argv", ["sources", "--config", "unused.json", *arguments])
    with pytest.raises(SystemExit):
        worker.Main()


@pytest.mark.parametrize("component", ("pledge", "unlock"))
def test_metadata_selection_on_other_families_fails_before_io(component):
    session = Session(_payload(_row()))
    with pytest.raises(ValueError):
        AuditCompanyStructuredPage(session, replace(_QUERY, component=component), observed_at=_NOW,
            timeout_seconds=8, deadline=60, monotonic=lambda: 0, financial_source_records=True)
    assert session.requests == []
