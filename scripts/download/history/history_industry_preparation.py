"""Inspect explicit current official industry candidates without publishing data."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from scripts.core.reporting import emit_report
from scripts.download.history.cninfo_industry_probe import CninfoIndustryProbeRequest, ProbeCninfoIndustryQueries
from scripts.download.history.official_industry import AuditOfficialIndustrySources, OfficialIndustryAuditRequest


def Main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--official-announcements", nargs='+')
    source.add_argument("--cninfo-query-codes", nargs='+')
    source.add_argument("--cninfo-cache-evidence", type=Path)
    parser.add_argument("--cninfo-cache-codes", nargs='+')
    parser.add_argument("--cninfo-cache-candidate", choices=("baseline", "batched", "compare"), default="baseline")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--cninfo-query-from", type=date.fromisoformat)
    parser.add_argument("--cninfo-query-to", type=date.fromisoformat)
    parser.add_argument("--industry-work-root", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy, OpenExternalHttp

    try:
        if args.cninfo_cache_evidence is not None:
            from scripts.download.history.industry_cache_measurement import IndustryCacheMeasurementRequest, MeasureIndustryCache
            from trader.infra.settings import LoadRuntimeSettings

            if args.runtime_config is None:
                raise ValueError("cache measurement requires --runtime-config")
            report = MeasureIndustryCache(
                IndustryCacheMeasurementRequest(
                    args.cninfo_cache_evidence, tuple(args.cninfo_cache_codes or ()),
                    args.industry_work_root, args.budget_seconds, args.cninfo_cache_candidate,
                ),
                BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config)),
            )
        elif args.cninfo_query_codes:
            with OpenExternalHttp(identity_content_encodings=("normal",)) as transport:
                report = ProbeCninfoIndustryQueries(
                    CninfoIndustryProbeRequest(
                        tuple(args.cninfo_query_codes), args.cninfo_query_from, args.cninfo_query_to,
                        args.industry_work_root, args.timeout_seconds, args.budget_seconds,
                    ),
                    transport,
                )
        else:
            with OpenExternalHttp() as transport:
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
