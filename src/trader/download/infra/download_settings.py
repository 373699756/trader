"""Read bounded history maintenance settings from one dedicated file."""

from __future__ import annotations

from pathlib import Path

from trader.download.domain.company_evidence import CompanyDocumentLimits, CompanySyncConfiguration
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.domain.qfq_window import QFQ_WINDOWS
from trader.infra.settings.data_paths import DataPaths
from trader.infra.settings.parser import (
    ConfigurationError,
    Integer,
    Number,
    ProfileMapping,
    ReadJsonObject,
    RequireExactKeys,
    RequireMapping,
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
        },
        "download.history",
    )
    qfq = RequireMapping(document, "online_qfq")
    RequireExactKeys(qfq, {"profiles"}, "download.online_qfq")
    windows = tuple(
        (
            profile_id,
            Integer(
                ProfileMapping(qfq.get("profiles"), profile_id, "download.online_qfq.profiles"),
                "sessions",
            ),
        )
        for profile_id in ("v2", "v3")
    )
    if windows != QFQ_WINDOWS:
        raise ConfigurationError("online qfq windows must match the active feature contracts")
    return HistorySyncConfiguration(
        archive_root=paths.history,
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
        training_root=paths.models,
    )


def LoadCompanySyncConfiguration(project_root: Path) -> CompanySyncConfiguration:
    document = _download_document(project_root)
    company = RequireMapping(document, "company")
    RequireExactKeys(
        company,
        {
            "budget_seconds",
            "structured_component_seconds",
            "announcements_deadline_fraction",
            "documents_deadline_fraction",
            "source_timeout_seconds",
            "document_parse_timeout_seconds",
            "request_interval_seconds",
            "document_failure_retry_seconds",
            "document_transient_retry_seconds",
            "document_limits",
        },
        "download.company",
    )
    announcements_fraction = Number(company, "announcements_deadline_fraction", minimum=0.01, maximum=0.98)
    documents_fraction = Number(company, "documents_deadline_fraction", minimum=0.02, maximum=0.99)
    if announcements_fraction >= documents_fraction:
        raise ConfigurationError("download.company deadlines must keep announcements before documents")
    limits = RequireMapping(company, "document_limits")
    RequireExactKeys(limits, {"max_bytes", "max_pages", "max_text_characters"}, "download.company.document_limits")
    return CompanySyncConfiguration(
        budget_seconds=Number(company, "budget_seconds", minimum=60.0, maximum=21600.0),
        structured_component_seconds=Number(company, "structured_component_seconds", minimum=1.0),
        announcements_deadline_fraction=announcements_fraction,
        documents_deadline_fraction=documents_fraction,
        source_timeout_seconds=Number(company, "source_timeout_seconds", minimum=0.1, maximum=8.0),
        document_parse_timeout_seconds=Number(company, "document_parse_timeout_seconds", minimum=0.1, maximum=30.0),
        request_interval_seconds=Number(company, "request_interval_seconds", minimum=1.0),
        document_failure_retry_seconds=Number(company, "document_failure_retry_seconds", minimum=1.0),
        document_transient_retry_seconds=Number(company, "document_transient_retry_seconds", minimum=1.0),
        document_limits=CompanyDocumentLimits(
            max_bytes=Integer(limits, "max_bytes", minimum=1, maximum=64 * 1024 * 1024),
            max_pages=Integer(limits, "max_pages", minimum=1, maximum=1000),
            max_text_characters=Integer(limits, "max_text_characters", minimum=80, maximum=2_000_000),
        ),
    )


def _download_document(project_root: Path) -> dict[str, object]:
    document = ReadJsonObject(project_root / "config" / "download.json")
    RequireExactKeys(document, {"history", "online_qfq", "company"}, "download")
    return document


__all__ = ["LoadCompanySyncConfiguration", "LoadHistorySyncConfiguration"]
