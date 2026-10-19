"""Read bounded history maintenance settings from one dedicated file."""

from __future__ import annotations

from pathlib import Path

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
    document = ReadJsonObject(project_root / "config" / "download.json")
    RequireExactKeys(document, {"history", "online_qfq"}, "download")
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


__all__ = ["LoadHistorySyncConfiguration"]
