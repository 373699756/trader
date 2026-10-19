"""Atomic publication of history reference files and their bound control snapshot."""

from __future__ import annotations

import re
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

_REFERENCE_SOURCE_CONTRACT = re.compile(r"history_ref_[0-9a-f]{64}_([0-9a-f]{16})")


@dataclass(frozen=True)
class HistoryPublication:
    root: Path
    identities: tuple[HistorySourceIdentity, HistoryCalendarIdentity, HistoryUniverseIdentity]
    snapshot: HistoryActiveSnapshot
    checkpoint: HistorySyncCheckpoint
    reference: HistoryReferenceSnapshot


def PublishHistorySnapshot(control: HistoryControlIndex, publication: HistoryPublication) -> None:
    """Publish the bound reference and snapshot pointer in the control transaction."""

    try:
        source, calendar, universe = publication.identities
        control.save_source(source)
        control.save_calendar(calendar)
        control.save_universe(universe)
        control.save_checkpoint(publication.checkpoint)
        control.publish_snapshot(publication.snapshot, reference=publication.reference)
    except BaseException:
        try:
            active_hash = control.load_state().active_snapshot_hash
        except HistoryControlError:
            raise
        if active_hash == publication.snapshot.content_hash:
            return
        raise


def IsReferenceOnlyChange(
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


__all__ = ["HistoryPublication", "IsReferenceOnlyChange", "PublishHistorySnapshot"]
