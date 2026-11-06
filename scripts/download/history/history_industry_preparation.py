"""Inspect explicit current official industry candidates without publishing data."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.core.reporting import emit_report
from scripts.download.history.official_industry import AuditOfficialIndustrySources, OfficialIndustryAuditRequest
from trader.infra.http_transport import RequestsHttpTransport


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-announcements", nargs='+', required=True)
    parser.add_argument("--industry-work-root", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    args = parser.parse_args()
    try:
        with RequestsHttpTransport() as transport:
            report = AuditOfficialIndustrySources(
                OfficialIndustryAuditRequest(
                    tuple(args.official_announcements), args.industry_work_root,
                    args.timeout_seconds, args.budget_seconds,
                ),
                transport,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        report = {"status": "failed", "summary": {"failure": type(exc).__name__, "production_eligible": False}}
    emit_report(report)
    return 1 if report["status"] == "failed" else 0
