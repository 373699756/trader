"""Restore durable document tasks from fully verified local originals."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteConnection, SqliteTransaction


@dataclass(frozen=True)
class _DocumentRecoveryState:
    stage: Literal["downloaded", "pending"]
    path: str | None
    payload_hash: bytes | None
    size: int | None


def RestoreCompanyDocumentTasks(
    connect: Callable[[], AbstractContextManager[SqliteConnection]],
    raw_files: CompanyRawFiles,
    root: Path,
    *,
    skip_unchanged: bool,
) -> None:
    """Keep per-task recovery; the explicit candidate omits only identical write transactions."""
    with connect() as connection:
        tasks = connection.run(
            "SELECT d.*,MIN(c.code) AS code FROM document_tasks t JOIN disclosures d "
            "ON d.source=t.source AND d.source_key=t.source_key AND d.record_hash=t.record_hash "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE t.stage<>'cleaned' GROUP BY d.id"
        ).fetchall()
    for row in tasks:
        item = DecodeCompanyDisclosure(row)
        local = raw_files.local(item)
        target = _DocumentRecoveryState(
            "downloaded" if local else "pending",
            str(local.path.relative_to(root)) if local else None,
            local.payload_hash if local else None,
            local.size if local else None,
        )
        identity = (item.source, item.identity, item.digest)
        with connect() as connection:
            if skip_unchanged:
                current = connection.run(
                    "SELECT stage,path,payload_hash,size FROM document_tasks "
                    "WHERE source=? AND source_key=? AND record_hash=?",
                    identity,
                ).fetchone()
                if current is not None and (
                    current["stage"] == target.stage
                    and current["path"] == target.path
                    and current["payload_hash"] == target.payload_hash
                    and current["size"] == target.size
                ):
                    continue
            with SqliteTransaction(connection, immediate=True):
                connection.run(
                    "UPDATE document_tasks SET stage=?,path=?,payload_hash=?,size=? "
                    "WHERE source=? AND source_key=? AND record_hash=?",
                    (target.stage, target.path, target.payload_hash, target.size, *identity),
                )


__all__ = ["RestoreCompanyDocumentTasks"]
