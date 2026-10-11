"""Merge shared ST evidence under a short, bounded cross-process lock."""

from __future__ import annotations

import time
from pathlib import Path

from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.infra.history_reference_files import read_st_evidence, write_st_evidence
from trader.infra.process_lock import ProcessLock, ProcessLockError


def merge_st_evidence(
    path: Path, incoming: tuple[HistoryStEvidence, ...], observed: tuple[HistoryStEvidence, ...] = ()
) -> tuple[HistoryStEvidence, ...]:
    lock = ProcessLock(path.with_name(".historical_st.lock"))
    deadline = time.monotonic() + 5.0
    while True:
        try:
            lock.acquire()
            break
        except ProcessLockError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("history_st_merge_busy") from exc
            time.sleep(0.05)
    try:
        existing = read_st_evidence(path)
        merged = {item.code: item for item in existing}
        baseline = {item.code: item for item in observed}
        for item in incoming:
            if baseline.get(item.code) == item:
                continue
            previous = merged.get(item.code)
            if previous is None:
                merged[item.code] = item
            elif _incoming_wins(previous, item, baseline.get(item.code)):
                merged[item.code] = item
        result = tuple(sorted(merged.values(), key=lambda item: item.code))
        if result != existing:
            write_st_evidence(path, result)
        return result
    finally:
        lock.release()


def _incoming_wins(
    previous: HistoryStEvidence, incoming: HistoryStEvidence, observed: HistoryStEvidence | None
) -> bool:
    if previous.status == "ever_st":
        return False
    if incoming.status == "ever_st" or incoming.checked_on > previous.checked_on:
        return True
    return incoming.checked_on == previous.checked_on and (previous == observed or incoming.status == "unknown")


__all__ = ["merge_st_evidence"]
