"""Atomic publication of history reference files and their bound control snapshot."""

from __future__ import annotations

import re
from dataclasses import dataclass

from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistoryControlState,
    HistorySnapshot,
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
    snapshot: HistorySnapshot
    checkpoint: HistorySyncCheckpoint
    reference: HistoryReferenceSnapshot


def PublishHistorySnapshot(control: HistoryControlIndex, publication: HistoryPublication) -> None:
    """Publish a snapshot after its reference and parents have been sealed."""

    try:
        control.save_checkpoint(publication.checkpoint)
        control.publish_snapshot(publication.snapshot, reference=publication.reference)
    except BaseException:
        try:
            published_hash = control.load_state().snapshot_hash
        except HistoryControlError:
            raise
        if published_hash == publication.snapshot.content_hash:
            return
        raise


def IsReferenceOnlyChange(
    state: HistoryControlState,
    current_snapshot: HistorySnapshot,
    source: HistorySourceIdentity,
    calendar: HistoryCalendarIdentity,
    universe: HistoryUniverseIdentity,
) -> bool:
    previous_source = next(
        (item for item in state.sources if item.content_hash == current_snapshot.source_identity_hash), None
    )
    previous_calendar = next(
        (item for item in state.calendars if item.content_hash == current_snapshot.calendar_hash), None
    )
    previous_universe = next(
        (item for item in state.universes if item.content_hash == current_snapshot.universe_hash), None
    )
    if previous_source is None or previous_calendar is None or previous_universe is None:
        raise HistoryControlError("history published snapshot parents are unavailable")
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
