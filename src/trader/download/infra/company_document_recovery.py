"""Restore durable document tasks from fully verified local originals."""

from __future__ import annotations

import zlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from trader.download.domain.company_announcement_numbers import CompanyAnnouncementNumbers, ExtractCompanyAnnouncementNumbers
from trader.download.infra.company_announcement_links import WriteCompanyAnnouncementNumbers
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.company_fact_inputs import DecodeCompanyDisclosure
from trader.download.infra.company_pipeline_queries import MarkCompanyDirty
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteConnection, SqliteTransaction


@dataclass(frozen=True)
class _DocumentRecoveryState:
    stage: Literal["downloaded", "pending"]
    path: str | None
    payload_hash: bytes | None
    size: int | None


@dataclass(frozen=True)
class _DocumentMaterialRepair:
    disclosure_id: int
    body_hash: bytes
    record_hash: str
    numbers: CompanyAnnouncementNumbers | None


def RepairCompanyDocumentFiles(
    connect: Callable[[], AbstractContextManager[SqliteConnection]],
    documents: CompanyDocumentFiles,
    check: Callable[[], None],
) -> None:
    """Validate bounded material batches outside writes, then recheck each reference."""
    check()
    with connect() as connection:
        maximum_id = connection.run("SELECT COALESCE(MAX(id),0) FROM disclosures").require_one()[0]
    cursor = 0
    while cursor < maximum_id:
        check()
        with connect() as connection:
            rows = connection.run(
                "SELECT id,body_hash,record_hash FROM disclosures "
                "WHERE body_retained=1 AND id>? AND id<=? ORDER BY id LIMIT 64",
                (cursor, maximum_id),
            ).fetchall()
        if not rows:
            break
        prepared: list[_DocumentMaterialRepair] = []
        for row in rows:
            check()
            try:
                text = documents.read(row[1], check=check)
            except FileNotFoundError:
                numbers = None
            except InterruptedError:
                raise
            except (OSError, ValueError, EOFError, zlib.error) as exc:
                raise RuntimeError("company_document_material_recovery_required") from exc
            else:
                numbers = ExtractCompanyAnnouncementNumbers(text)
            prepared.append(_DocumentMaterialRepair(row[0], row[1], row[2], numbers))
        check()
        with connect() as connection, SqliteTransaction(connection, immediate=True):
            for item in prepared:
                check()
                current = connection.run(
                    "SELECT 1 FROM disclosures WHERE id=? AND record_hash=? AND body_hash=? AND body_retained=1",
                    (item.disclosure_id, item.record_hash, item.body_hash),
                ).fetchone()
                if current is None:
                    continue
                if item.numbers is not None:
                    WriteCompanyAnnouncementNumbers(
                        connection, item.disclosure_id, item.record_hash, item.body_hash, item.numbers,
                    )
                    continue
                connection.run(
                    "UPDATE disclosures SET body_checked=0,body_retained=0,body_hash=NULL WHERE id=?",
                    (item.disclosure_id,),
                )
                connection.run("DELETE FROM document_extractions WHERE disclosure_id=?", (item.disclosure_id,))
                MarkCompanyDirty(connection, tuple(row[0] for row in connection.run(
                    "SELECT code FROM disclosure_codes WHERE disclosure_id=?", (item.disclosure_id,),
                )))
        cursor = rows[-1][0]


def RestoreCompanyDocumentTasks(
    connect: Callable[[], AbstractContextManager[SqliteConnection]],
    raw_files: CompanyRawFiles,
    root: Path,
    *,
    skip_unchanged: bool,
    check: Callable[[], None],
) -> None:
    """Keep per-task recovery; the explicit candidate omits only identical write transactions."""
    with connect() as connection:
        tasks = connection.run(
            "SELECT d.*,MIN(c.code) AS code FROM document_tasks t JOIN disclosures d "
            "ON d.source=t.source AND d.source_key=t.source_key AND d.record_hash=t.record_hash "
            "JOIN disclosure_codes c ON c.disclosure_id=d.id WHERE t.stage<>'cleaned' GROUP BY d.id"
        ).fetchall()
    for row in tasks:
        check()
        item = DecodeCompanyDisclosure(row)
        local = raw_files.local(item, check=check)
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


__all__ = [
    "RepairCompanyDocumentFiles",
    "RestoreCompanyDocumentTasks",
]
