"""Read bounded history maintenance settings from one dedicated file."""

from __future__ import annotations

from pathlib import Path

from trader.download.domain.company_evidence import CompanyDocumentLimits, CompanySyncConfiguration
from trader.download.domain.history_sync import HistorySyncConfiguration, HistoryWriteBatchPolicy
from trader.download.domain.qfq_window import QfqWindowConfiguration
from trader.infra.settings.data_paths import DataPaths
from trader.infra.settings.parser import (
    ConfigurationError,
    Integer,
    Number,
    ProfileMapping,
    ReadJsonObject,
    RequireExactKeys,
    RequireMapping,
    Text,
)


def LoadHistorySyncConfiguration(project_root: Path, paths: DataPaths) -> HistorySyncConfiguration:
    document = _download_document(project_root)
    history = RequireMapping(document, "history")
    RequireExactKeys(
        history,
        {
            "requested_sessions",
            "reread_sessions",
            "download_batch_size",
            "history_workers",
            "minimum_free_bytes",
            "supplier_timeout_seconds",
            "supplier_retries",
            "query_interval_seconds",
            "cancellation_grace_seconds",
            "progress_heartbeat_seconds",
            "write_batches",
        },
        "download.history",
    )
    write_batches = RequireMapping(history, "write_batches")
    RequireExactKeys(write_batches, {"maximum_codes", "maximum_cells", "hdd", "ssd"}, "download.history.write_batches")
    hdd = RequireMapping(write_batches, "hdd")
    ssd = RequireMapping(write_batches, "ssd")
    batch_keys = {"fresh_codes", "fresh_cells", "incremental_codes", "incremental_cells"}
    RequireExactKeys(hdd, batch_keys, "download.history.write_batches.hdd")
    RequireExactKeys(ssd, batch_keys, "download.history.write_batches.ssd")
    return HistorySyncConfiguration(
        history_root=paths.history,
        reference_root=paths.reference,
        sessions=Integer(history, "requested_sessions", minimum=1, maximum=2000),
        reread_sessions=Integer(history, "reread_sessions", minimum=1),
        download_batch_size=Integer(history, "download_batch_size", minimum=1),
        history_workers=Integer(history, "history_workers", minimum=1),
        minimum_free_bytes=Integer(history, "minimum_free_bytes", minimum=0),
        supplier_timeout_seconds=Number(history, "supplier_timeout_seconds", minimum=0.001),
        supplier_retries=Integer(history, "supplier_retries", minimum=0),
        query_interval_seconds=Number(history, "query_interval_seconds", minimum=1.5),
        cancellation_grace_seconds=Number(history, "cancellation_grace_seconds", minimum=0.001),
        progress_heartbeat_seconds=Number(history, "progress_heartbeat_seconds", minimum=0.001),
        write_batches=HistoryWriteBatchPolicy(
            maximum_codes=Integer(write_batches, "maximum_codes", minimum=1, maximum=64),
            maximum_cells=Integer(write_batches, "maximum_cells", minimum=1, maximum=32768),
            hdd_fresh_codes=Integer(hdd, "fresh_codes", minimum=1),
            hdd_fresh_cells=Integer(hdd, "fresh_cells", minimum=1),
            hdd_incremental_codes=Integer(hdd, "incremental_codes", minimum=1),
            hdd_incremental_cells=Integer(hdd, "incremental_cells", minimum=1),
            ssd_fresh_codes=Integer(ssd, "fresh_codes", minimum=1),
            ssd_fresh_cells=Integer(ssd, "fresh_cells", minimum=1),
            ssd_incremental_codes=Integer(ssd, "incremental_codes", minimum=1),
            ssd_incremental_cells=Integer(ssd, "incremental_cells", minimum=1),
        ),
    )


def LoadQfqWindowConfiguration(project_root: Path) -> QfqWindowConfiguration:
    document = _download_document(project_root)
    qfq = RequireMapping(document, "online_qfq")
    RequireExactKeys(
        qfq,
        {"profiles", "workers", "source_timeout_seconds", "source_retries"},
        "download.online_qfq",
    )
    profiles = qfq.get("profiles")
    if not isinstance(profiles, list) or len(profiles) != 2:
        raise ConfigurationError("download.online_qfq.profiles must define only v2 and v3")
    v2 = ProfileMapping(profiles, "v2", "download.online_qfq.profiles")
    v3 = ProfileMapping(profiles, "v3", "download.online_qfq.profiles")
    RequireExactKeys(v2, {"sessions"}, "download.online_qfq.profiles.v2")
    RequireExactKeys(v3, {"sessions"}, "download.online_qfq.profiles.v3")
    try:
        return QfqWindowConfiguration(
            v2_sessions=Integer(v2, "sessions", minimum=1),
            v3_sessions=Integer(v3, "sessions", minimum=1),
            workers=Integer(qfq, "workers", minimum=1, maximum=12),
            source_timeout_seconds=Number(qfq, "source_timeout_seconds", minimum=0.1, maximum=120.0),
            source_retries=Integer(qfq, "source_retries", minimum=0, maximum=2),
        )
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def LoadCompanySyncConfiguration(project_root: Path) -> CompanySyncConfiguration:
    document = _download_document(project_root)
    company = RequireMapping(document, "company")
    RequireExactKeys(
        company,
        {
            "source_timeout_seconds",
            "document_parse_timeout_seconds",
            "request_interval_seconds",
            "directory_batch_pages",
            "document_batch_size",
            "document_workers",
            "document_failure_retry_seconds",
            "document_transient_retry_seconds",
            "document_ocr_timeout_seconds",
            "document_ocr_max_pages",
            "document_ocr_dpi",
            "document_ocr_languages",
            "temporary_text_capacity_bytes",
            "temporary_disk_reserve_bytes",
            "document_limits",
            "extended_document_limits",
        },
        "download.company",
    )
    limits = RequireMapping(company, "document_limits")
    expanded = RequireMapping(company, "extended_document_limits")
    RequireExactKeys(limits, {"max_bytes", "max_pages", "max_text_characters"}, "download.company.document_limits")
    RequireExactKeys(
        expanded,
        {"max_bytes", "max_pages", "max_text_characters"},
        "download.company.extended_document_limits",
    )
    return CompanySyncConfiguration(
        source_timeout_seconds=Number(company, "source_timeout_seconds", minimum=0.1, maximum=8.0),
        document_parse_timeout_seconds=Number(company, "document_parse_timeout_seconds", minimum=0.1, maximum=30.0),
        request_interval_seconds=Number(company, "request_interval_seconds", minimum=1.0),
        directory_batch_pages=Integer(company, "directory_batch_pages", minimum=1, maximum=100),
        document_batch_size=Integer(company, "document_batch_size", minimum=1, maximum=200),
        document_workers=Integer(company, "document_workers", minimum=1, maximum=2),
        document_failure_retry_seconds=Number(company, "document_failure_retry_seconds", minimum=1.0),
        document_transient_retry_seconds=Number(company, "document_transient_retry_seconds", minimum=1.0),
        document_ocr_timeout_seconds=Number(company, "document_ocr_timeout_seconds", minimum=1.0, maximum=600.0),
        document_ocr_max_pages=Integer(company, "document_ocr_max_pages", minimum=1, maximum=200),
        document_ocr_dpi=Integer(company, "document_ocr_dpi", minimum=72, maximum=300),
        document_ocr_languages=Text(company, "document_ocr_languages"),
        temporary_text_capacity_bytes=Integer(company, "temporary_text_capacity_bytes", minimum=1),
        temporary_disk_reserve_bytes=Integer(company, "temporary_disk_reserve_bytes", minimum=1),
        document_limits=CompanyDocumentLimits(
            max_bytes=Integer(limits, "max_bytes", minimum=1, maximum=64 * 1024 * 1024),
            max_pages=Integer(limits, "max_pages", minimum=1, maximum=1000),
            max_text_characters=Integer(limits, "max_text_characters", minimum=80, maximum=2_000_000),
        ),
        extended_document_limits=CompanyDocumentLimits(
            max_bytes=Integer(expanded, "max_bytes", minimum=1, maximum=64 * 1024 * 1024),
            max_pages=Integer(expanded, "max_pages", minimum=1, maximum=1000),
            max_text_characters=Integer(expanded, "max_text_characters", minimum=80, maximum=2_000_000),
        ),
    )


def _download_document(project_root: Path) -> dict[str, object]:
    document = ReadJsonObject(project_root / "config" / "download.json")
    RequireExactKeys(document, {"history", "online_qfq", "company"}, "download")
    return document


__all__ = ["LoadCompanySyncConfiguration", "LoadHistorySyncConfiguration", "LoadQfqWindowConfiguration"]
