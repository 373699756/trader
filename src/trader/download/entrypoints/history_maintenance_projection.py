"""JSON projection owned by the historical-maintenance CLI boundary."""

from __future__ import annotations

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus


def ProjectHistoryMaintenanceStatus(status: HistoryMaintenanceStatus) -> dict[str, object]:
    return {
        "schema_version": "history_maintenance_status",
        "state": status.state,
        "reason": status.reason,
        "history_root": str(status.history_root),
        "selected_baseline_source": status.selected_baseline_source,
        "efficient_daily_source": status.efficient_daily_source,
        "snapshot_hash": status.snapshot_hash,
        "data_cutoff": status.data_cutoff.isoformat() if status.data_cutoff is not None else None,
        "label_cutoff": status.label_cutoff.isoformat() if status.label_cutoff is not None else None,
        "unresolved_price_cells": status.unresolved_price_cells,
        "industry": None if status.industry_readiness is None else {
            "source": status.industry_readiness.source,
            "requested_codes": status.industry_readiness.requested_codes,
            "candidate_codes": status.industry_readiness.candidate_codes,
            "point_in_time_codes": status.industry_readiness.point_in_time_codes,
            "complete": status.industry_readiness.ready,
            "training_industry_ready": status.industry_readiness.ready,
            "failure_category": status.industry_readiness.failure_category,
        },
        "price_review": None if status.price_review is None else {
            "examined_codes": status.price_review.examined_codes,
            "physical_requests": status.price_review.physical_requests,
            "remaining_inferred_codes": status.price_review.remaining_inferred_codes,
            "discovery_pending": status.price_review.discovery_pending,
            "exhaustion_reason": status.price_review.exhaustion_reason,
        },
        "price_gaps": None
        if status.price_gap_summary is None
        else {
            "stocks": status.price_gap_summary.stocks,
            "cells": status.price_gap_summary.cells,
            "first_date": str(status.price_gap_summary.first_date) if status.price_gap_summary.first_date else None,
            "last_date": str(status.price_gap_summary.last_date) if status.price_gap_summary.last_date else None,
            "reasons": [{"reason": item.reason, "cells": item.cells} for item in status.price_gap_summary.reasons],
        },
    }


__all__ = ["ProjectHistoryMaintenanceStatus"]
