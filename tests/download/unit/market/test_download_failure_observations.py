"""Failure observation precedes diagnostic I/O and preserves bounded safe evidence."""

import errno
import json
import sqlite3
from dataclasses import FrozenInstanceError
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.download_failure import DownloadFailureContext, DownloadValidationFailure
from trader.download.domain.company_evidence import CompanyDocumentFailure, CompanyOcrDiagnostic
from trader.download.infra.download_failure_diagnostics import DownloadFailureDiagnostics
from trader.infra.http_contracts import HttpStatusError

NOW = datetime(2026, 11, 7, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_pending_failure_is_observed_before_persistence_and_saved_correlates_the_same_record():
    observations, calls, old_output = [], [], []

    def persist(record):
        assert observations[-1].persistence == "pending"
        calls.append(record)

    recorder = DownloadFailureDiagnostics(persist, lambda: NOW, report=old_output.append, owner="company", observe=observations.append)
    context = DownloadFailureContext(
        "company_document", operation_id=17, operation_kind="ocr", component="risk_documents", object_id="cninfo:announcement_1",
        revision="a" * 64, market="sse", page=4, page_size=30, queued_seconds=2.5,
        raw_saved=True, text_extracted=False, collection_committed=False,
        name="测试股票", title="关于变更\n公告|边界",
    )
    recorder(context, DownloadValidationFailure("company_source_schema_invalid", "result", "mapping", "NoneType"))
    assert [item.persistence for item in observations] == ["pending", "saved"]
    assert observations[0].record is observations[1].record is calls[0]
    assert calls[0].run_id == recorder.run_id and old_output == []
    assert "operation_id=17" in calls[0].diagnostic and "object_id=cninfo:announcement_1" in calls[0].diagnostic
    assert "component=risk_documents" in calls[0].diagnostic
    assert "field=result expected=mapping actual=NoneType" in calls[0].diagnostic
    assert "raw_saved=True" in calls[0].diagnostic and "collection_committed=False" in calls[0].diagnostic
    assert "publication_committed=" not in calls[0].diagnostic
    assert "name=测试股票" in calls[0].diagnostic and "title=关于变更 公告 边界" in calls[0].diagnostic
    with pytest.raises(FrozenInstanceError):
        observations[0].persistence = "failed"


def test_diagnostic_write_failure_keeps_original_and_safe_secondary_evidence_without_duplicate_output(caplog):
    observations, old_output = [], []

    def unavailable(_record):
        with sqlite3.connect(":memory:") as connection:
            connection.execute("SELECT * FROM no_such_table")

    recorder = DownloadFailureDiagnostics(unavailable, lambda: NOW, report=old_output.append, observe=observations.append)
    try:
        raise OSError(errno.ENOSPC, "private-token /private/raw.pdf")
    except OSError as exc:
        recorder(DownloadFailureContext("company_document"), exc)
    assert [item.persistence for item in observations] == ["pending", "failed"]
    assert observations[0].record is observations[1].record
    assert f"errno_{errno.ENOSPC}" in observations[1].record.diagnostic
    assert "SQLITE_ERROR" in observations[1].recording_diagnostic
    assert "test_download_failure_observations.py" in observations[1].recording_diagnostic
    safe = str(observations)
    assert "private-token" not in safe and "/private/" not in safe and "no_such_table" not in safe
    assert old_output == [] and "download_failure_recording_failed" not in caplog.text


def test_wrapped_http_and_validation_evidence_keeps_real_status_and_locations_without_json_payload():
    observations = []
    recorder = DownloadFailureDiagnostics(lambda record: None, lambda: NOW, observe=observations.append)
    try:
        try:
            raise HttpStatusError(503)
        except HttpStatusError as exc:
            raise DownloadValidationFailure("company_source_schema_invalid", "root", "mapping", "str") from exc
    except DownloadValidationFailure as exc:
        recorder(DownloadFailureContext("company_directory"), exc)
    record = observations[0].record
    assert "HTTP_503" in record.diagnostic and "test_download_failure_observations.py" in record.diagnostic
    assert "http_503" in record.causes and "field=root expected=mapping actual=str" in record.diagnostic
    try:
        json.loads("private-payload /private/raw.pdf")
    except json.JSONDecodeError as exc:
        recorder(DownloadFailureContext("company_directory"), exc)
    assert "private-payload" not in str(observations) and "/private/" not in str(observations)


def test_unsafe_validation_values_cannot_be_attached_as_scalar_differences():
    with pytest.raises(ValueError, match="download_validation_difference_invalid"):
        DownloadValidationFailure("company_source_schema_invalid", "root", "mapping", "/private/document.pdf")


@pytest.mark.parametrize("reason,diagnostic,confidence", [
    ("disclosure_document_ocr_low_quality", CompanyOcrDiagnostic("quality", 2, 2, 14, 31.25, 6), "31.250"),
    ("disclosure_document_ocr_timeout", CompanyOcrDiagnostic("render", 3, 12, 182, None, 6), "未采集"),
])
def test_ocr_failure_keeps_existing_typed_diagnostics_without_original_payload(reason, diagnostic, confidence):
    observations = []
    recorder = DownloadFailureDiagnostics(lambda record: None, lambda: NOW, observe=observations.append)
    original_hash = bytes.fromhex("ab" * 32)
    error = RuntimeError("company_document_execution_failed")
    error.__cause__ = CompanyDocumentFailure(reason, payload_hash=original_hash, diagnostic=diagnostic)
    recorder(DownloadFailureContext("company_ocr"), error)
    detail = observations[0].record.diagnostic
    assert f"ocr_phase={diagnostic.phase}" in detail
    assert f"ocr_page={diagnostic.page} ocr_pages={diagnostic.pages}" in detail
    assert f"ocr_characters={diagnostic.characters} ocr_confidence={confidence}" in detail
    assert "ocr_segmentation_mode=6" in detail
    assert original_hash.hex() not in str(observations)
