"""Physical admission for immutable SQLite audit partitions, including WAL sidecars."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


class ResearchTraceCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class TracePhysicalBudget:
    partition_bytes: int
    total_bytes: int

    def require(self, databases: tuple[Path, ...], target: Path, *, pending_bytes: int = 0) -> None:
        files = tuple(dict.fromkeys((*databases, target)))
        seen: set[tuple[int, int]] = set()
        total = 0
        target_size = 0
        for database in files:
            for suffix in ("", "-wal", "-shm", "-journal"):
                path = Path(f"{database}{suffix}")
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                size = max(stat.st_size, getattr(stat, "st_blocks", 0) * 512)
                if database == target:
                    target_size += size
                identity = (stat.st_dev, stat.st_ino)
                if identity not in seen:
                    total += size
                    seen.add(identity)
        if target_size + pending_bytes > self.partition_bytes:
            raise ResearchTraceCapacityError("research trace physical partition capacity exhausted")
        if total + pending_bytes > self.total_bytes:
            raise ResearchTraceCapacityError("research trace physical total capacity exhausted")

    def require_commit(self, conn: sqlite3.Connection, databases: tuple[Path, ...], target: Path) -> None:
        # With cache spilling disabled, a transaction emits at most one WAL frame per page.
        # Reserve the whole database as a conservative bound, including index splits and growth.
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
        logical_size = pages * page_size
        target_stat = target.stat()
        block = max(4096, getattr(target_stat, "st_blksize", 4096))
        growth = max(0, logical_size - target_stat.st_size)
        wal_reserve = 32 + pages * (page_size + 24)
        shm_reserve = (pages // 4096 + 1) * 32768
        pending = sum(((size + block - 1) // block) * block for size in (growth, wal_reserve, shm_reserve))
        self.require(databases, target, pending_bytes=pending)


__all__ = ["ResearchTraceCapacityError", "TracePhysicalBudget"]
