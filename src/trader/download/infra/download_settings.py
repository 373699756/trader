"""Read bounded history maintenance settings from one dedicated file."""

from __future__ import annotations

from pathlib import Path

from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.domain.qfq_window import QFQ_WINDOWS
from trader.infra.settings.parser import (
    ConfigurationError,
    integer,
    mapping,
    number,
    profile_mapping,
    read_json_object,
    require_exact_keys,
)


def load_history_sync_configuration(project_root: Path, reference_root: Path) -> HistorySyncConfiguration:
    document = read_json_object(project_root / "config" / "download.json")
    require_exact_keys(document, {"history", "online_qfq"}, "download")
    history = mapping(document, "history")
    require_exact_keys(
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
    qfq = mapping(document, "online_qfq")
    require_exact_keys(qfq, {"profiles"}, "download.online_qfq")
    windows = tuple(
        (
            profile_id,
            integer(
                profile_mapping(qfq.get("profiles"), profile_id, "download.online_qfq.profiles"),
                "sessions",
            ),
        )
        for profile_id in ("v2", "v3")
    )
    if windows != QFQ_WINDOWS:
        raise ConfigurationError("online qfq windows must match the active feature contracts")
    return HistorySyncConfiguration(
        archive_root=project_root / "data" / "history",
        reference_root=reference_root,
        sessions=integer(history, "requested_sessions", minimum=1, maximum=2000),
        reread_sessions=integer(history, "reread_sessions", minimum=1),
        download_batch_size=integer(history, "download_batch_size", minimum=1),
        history_workers=integer(history, "history_workers", minimum=1),
        minimum_free_bytes=integer(history, "minimum_free_bytes", minimum=0),
        supplier_timeout_seconds=number(history, "supplier_timeout_seconds", minimum=0.001),
        supplier_retries=integer(history, "supplier_retries", minimum=0),
        query_interval_seconds=number(history, "query_interval_seconds", minimum=1.5),
        cancellation_grace_seconds=number(history, "cancellation_grace_seconds", minimum=0.001),
        progress_heartbeat_seconds=number(history, "progress_heartbeat_seconds", minimum=0.001),
        training_root=project_root / "data" / "train",
    )


__all__ = ["load_history_sync_configuration"]
