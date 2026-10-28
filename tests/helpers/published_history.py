"""Build real published inputs in monthly batches for reader-only tests.

Download/retry/atomicity tests must still exercise the complete download use case.
This builder uses production codecs and FULL transactions, not SQL shortcuts or
mocked durability, but does not repeat per-stock network/checkpoint orchestration.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tests.reference_fixtures import PublishHistoryReferenceFixture
from trader.download.domain.baostock_daily import BaoStockDailyCell
from trader.download.domain.history_control import (
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySnapshot,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistoryUniverseIdentity,
)
from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_prices import HistoryPricePartition


def publish_history_fixture(
    root: Path,
    context: HistorySupplierContext,
    cells: tuple[BaoStockDailyCell, ...],
    observed_at: datetime,
) -> HistorySnapshot:
    """Publish one complete, isolated fixture through real persistence boundaries."""
    assert not root.exists(), "fixture publication must not overwrite an existing history"
    securities = {item.code: item for item in context.universe}
    reference = HistoryReferenceSnapshot(tuple(sorted(securities)), context.industry_intervals, context.st_evidence)
    source = HistorySourceIdentity(
        "tencent_baostock", "history_daily", f"history_ref_{reference.content_hash}_{'0' * 16}", observed_at
    )
    calendar = HistoryCalendarIdentity(context.calendar.open_dates, source.content_hash)
    universe = HistoryUniverseIdentity(
        tuple(
            HistorySecurityIdentity(item.code, item.name, item.board, item.listed_on, item.delisted_on)
            for item in context.universe
        ),
        source.content_hash,
    )
    grouped: dict[tuple[int, int], list[HistoryRevision]] = defaultdict(list)
    for cell in cells:
        grouped[(cell.trade_date.year, cell.trade_date.month)].append(
            HistoryRevision(1, securities[cell.code].board, cell, False, None, None)
        )
    partitions: list[HistorySnapshotPartition] = []
    for (year, month), revisions in sorted(grouped.items()):
        revisions.sort(key=lambda item: (item.trade_date, item.code, item.first_seen_sequence, item.revision_id))
        prices = HistoryPricePartition(root / f"prices_{year:04d}.sqlite3", year, month)
        prices.initialize()
        prices.save_revisions(tuple(revisions))
        partitions.append(prices.seal())
    reference_path = root.parent / "reference" / "company_st.sqlite3"
    publication_id = PublishHistoryReferenceFixture(
        reference_path,
        reference,
        securities=context.universe,
        open_dates=tuple(sorted({*context.calendar.open_dates, observed_at.date()})),
    )
    control = HistoryControlIndex(root / "catalog.sqlite3", reference_path=reference_path)
    control.initialize()
    control.save_source(source)
    control.save_reference(reference, publication_id)
    control.save_calendar(calendar, publication_id)
    control.save_universe(universe, publication_id)
    snapshot = HistorySnapshot(
        1,
        calendar.open_dates[-1],
        calendar.open_dates[-2] if len(calendar.open_dates) > 1 else calendar.open_dates[-1],
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        tuple(partitions),
    )
    control.publish_snapshot(snapshot, reference=reference)
    return snapshot
