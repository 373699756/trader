"""Atomic publication of history reference files and their bound control snapshot."""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistoryControlState,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
)
from trader.download.infra.history_reference_files import (
    history_industry_mapping_path,
    history_reference_path,
    write_history_reference,
)

_REFERENCE_SOURCE_CONTRACT = re.compile(r"history_ref_[0-9a-f]{64}_([0-9a-f]{16})")
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryPublication:
    root: Path
    identities: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]
    snapshot: HistoryActiveSnapshot
    checkpoint: HistorySyncCheckpoint
    reference: HistoryReferenceSnapshot


def publish_history_snapshot(control: HistoryControlIndex, publication: HistoryPublication) -> None:
    """Publish a reference file and control identity as one recoverable operation."""

    write_history_reference(
        history_reference_path(publication.root, publication.reference.content_hash), publication.reference
    )
    mapping_path = history_industry_mapping_path(publication.root)
    rollback = mapping_path.with_name(".industry_mapping.rollback.json")
    rollback.unlink(missing_ok=True)
    if mapping_path.is_file():
        rollback.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mapping_path, rollback)
        _fsync_file(rollback)
    try:
        write_history_reference(mapping_path, publication.reference)
        source, calendar, universe = publication.identities
        control.save_source(source)
        control.save_calendar(calendar)
        control.save_universe(universe)
        control.save_checkpoint(publication.checkpoint)
        control.publish_snapshot(publication.snapshot)
    except BaseException:
        try:
            active_hash = control.load_state().active_snapshot_hash
        except HistoryControlError:
            raise
        if active_hash == publication.snapshot.content_hash:
            _cleanup_rollback(rollback)
            return
        if rollback.is_file():
            os.replace(rollback, mapping_path)
            _fsync_directory(mapping_path.parent)
        else:
            mapping_path.unlink(missing_ok=True)
        raise
    _cleanup_rollback(rollback)


def _cleanup_rollback(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Publication is committed. Recovery can retry cleanup, never undo active prices.
        _LOGGER.warning("history_reference_cleanup_pending")


def is_reference_only_change(
    state: HistoryControlState,
    active: HistoryActiveSnapshot,
    source: HistorySourceIdentity,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
) -> bool:
    previous_source = next((item for item in state.sources if item.content_hash == active.source_identity_hash), None)
    previous_calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    previous_universe = next((item for item in state.universes if item.content_hash == active.universe_hash), None)
    if previous_source is None or previous_calendar is None or previous_universe is None:
        raise HistoryControlError("history active snapshot parents are unavailable")
    previous_contract = _REFERENCE_SOURCE_CONTRACT.fullmatch(previous_source.supplier_contract)
    current_contract = _REFERENCE_SOURCE_CONTRACT.fullmatch(source.supplier_contract)
    return bool(
        previous_contract is not None
        and current_contract is not None
        and previous_source.supplier_contract != source.supplier_contract
        and previous_contract.group(1) == current_contract.group(1)
        and previous_calendar.open_dates == calendar.open_dates
        and previous_universe.securities == universe.securities
    )


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["HistoryPublication", "is_reference_only_change", "publish_history_snapshot"]
