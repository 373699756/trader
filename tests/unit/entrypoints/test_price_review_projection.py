from __future__ import annotations

from datetime import date
from pathlib import Path

from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus, HistoryPriceReviewSummary
from trader.download.domain.qfq_window import QfqPriceReviewSummary, QfqUpdateResult
from trader.download.entrypoints.sync_projection import ProjectSyncResult


def test_review_budget_exhaustion_is_visible_without_reclassifying_market_success() -> None:
    market = QfqUpdateResult(
        date(2026, 11, 5), completed_codes=2,
        price_review=QfqPriceReviewSummary(1, 16, 3, "request_limit", discovery_completed=True),
    )
    projected = ProjectSyncResult(SyncResult((SyncTaskResult("market", "completed", market=market),)))
    assert projected["status"] == "completed"
    assert projected["tasks"][0]["failure_reason"] is None
    assert projected["tasks"][0]["price_review"] == {
        "examined_codes": 1, "physical_requests": 16,
        "remaining_inferred_codes": 3, "exhaustion_reason": "request_limit",
        "discovery_completed": True,
    }


def test_zero_discovered_history_items_never_claims_complete_inventory(tmp_path: Path) -> None:
    history = HistoryMaintenanceStatus(
        "already_current", None, tmp_path, "tencent", None, "a" * 64,
        date(2026, 11, 5), date(2026, 11, 4),
        price_review=HistoryPriceReviewSummary(8, 0, "code_limit", 0, True),
    )
    projected = ProjectSyncResult(SyncResult((SyncTaskResult("history", "completed", history=history),)))
    summary = projected["tasks"][0]["history"]["price_review"]
    assert summary["remaining_inferred_codes"] == 0
    assert summary["discovery_pending"] is True
    assert summary["exhaustion_reason"] == "code_limit"
