"""Read-only admission audit; supplier coverage never proves absence of unknown risk."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

from .reporting import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceReader  # noqa: E402
from trader.download.infra.reference_publication import ReferencePublication  # noqa: E402
from trader.infra.settings.loading import LoadLongWatchlist, LoadRuntimeSettings, LoadStrategySettings  # noqa: E402
from trader.infra.sqlite_connection import SqliteConnectionOptions  # noqa: E402
from trader.recommendation.domain.market.eligibility import (  # noqa: E402
    IsActivePermanentEligibilityReason,
    IssuerEligibilityReason,
)
from trader.recommendation.domain.market.research import FinancialReport, LongResearchPolicy  # noqa: E402
from trader.recommendation.infra.market_data.published_company_research import PublishedCompanyResearch  # noqa: E402


@dataclass(frozen=True)
class Admission:
    code: str
    disposition: str
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
    latest_period: str = ""
    annual_periods: tuple[str, ...] = ()
    evidence_as_of: str = ""


def required_period(as_of: date) -> date:
    """Latest period whose statutory disclosure deadline has elapsed."""
    if as_of >= date(as_of.year, 11, 1):
        return date(as_of.year, 9, 30)
    if as_of >= date(as_of.year, 9, 1):
        return date(as_of.year, 6, 30)
    if as_of >= date(as_of.year, 5, 1):
        return date(as_of.year, 3, 31)
    return date(as_of.year - 1, 9, 30)


def financial_admission(code: str, history: tuple[FinancialReport, ...], complete: bool, as_of: date) -> Admission:
    annual = tuple(report for report in history if (report.report_date.month, report.report_date.day) == (12, 31))
    latest = max(history, key=lambda report: (report.report_date, report.published_at), default=None)
    latest_report_loss = latest is not None and any(
        value is not None and value < 0 for value in (latest.parent_net_profit, latest.core_net_profit)
    )
    reasons: list[str] = []
    evidence: tuple[str, ...] = ()
    if latest_report_loss:
        assert latest is not None
        reasons.append("latest_disclosed_report_loss")
        evidence = (f"financial:{code}:{latest.report_date.isoformat()}",)
    if reasons:
        disposition = "excluded"
    else:
        if not complete:
            reasons.append("financial_report_unavailable")
        if latest is None or latest.report_date < required_period(as_of):
            reasons.append("latest_report_missing")
        if latest is None or any(
            value is None or not math.isfinite(value) for value in (latest.parent_net_profit, latest.core_net_profit)
        ):
            reasons.append("profit_fields_missing")
        disposition = "pending" if reasons else "financial_pass"
    return Admission(
        code,
        disposition,
        tuple(reasons),
        evidence,
        latest.report_date.isoformat() if latest else "",
        tuple(report.report_date.isoformat() for report in annual),
    )


def historical_exclusions(paths: tuple[Path, ...], observed_at: datetime) -> dict[str, tuple[str, ...]]:
    """Read historical fact databases without migrating or writing active state."""
    evidence: dict[str, list[str]] = {}
    for path in paths:
        with SqliteSession(path, SqliteConnectionOptions(read_only=True)) as connection, SqliteTransaction(connection):
            for code, reason, effective_at, evidence_id in connection.run(
                "SELECT code, reason, effective_at, evidence_id FROM issuer_eligibility_facts"
            ):
                eligibility_reason = IssuerEligibilityReason(str(reason))
                if not IsActivePermanentEligibilityReason(eligibility_reason):
                    continue
                effective = datetime.fromisoformat(effective_at)
                if effective.tzinfo is None:
                    raise ValueError("eligibility evidence time must be timezone-aware")
                if effective <= observed_at:
                    evidence.setdefault(code, []).append(evidence_id)
    return {code: tuple(sorted(set(ids))) for code, ids in evidence.items()}


def audit_code(
    code: str,
    *,
    observed_at: datetime,
    policy: LongResearchPolicy,
    timeout: float,
    exclusions: dict[str, tuple[str, ...]],
    evidence_database: Path,
    open_dates: tuple[date, ...],
) -> Admission:
    if code in exclusions:
        return Admission(code, "excluded", ("existing_permanent_exclusion",), exclusions[code])
    reader = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(evidence_database),
        wall_clock=lambda: observed_at,
        open_dates=lambda: open_dates,
        monotonic=time.monotonic,
    )
    reader.initialize((code,), cancelled=lambda: False)
    observation = reader.load((code,), observed_at, include_structured=True).get(code)
    if observation is None:
        return Admission(code, "pending", ("company_snapshot_missing",))
    admission = financial_admission(
        code, observation.financial_history, observation.financial_history_complete, observed_at.date()
    )
    if admission.disposition != "financial_pass":
        return admission
    if observation.corporate_risk_facts:
        return replace(
            admission,
            disposition="pending",
            reasons=("recent_risk_review",),
            evidence_ids=tuple(item.evidence_id for item in observation.corporate_risk_facts),
        )
    return replace(
        admission,
        disposition="pending",
        reasons=("long_qualification_unverified",)
        if observation.corporate_risk_window_complete
        else ("corporate_risk_window_unavailable",),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist", type=Path, default=PROJECT_ROOT / "config/long_watchlist.json")
    parser.add_argument("--strategy", type=Path, default=PROJECT_ROOT / "config/strategy.json")
    parser.add_argument("--eligibility-evidence", type=Path, action="append", default=[])
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--company-evidence-database", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or not 0 < args.timeout_seconds <= 15:
        parser.error("workers must be 1..8 and request timeout must be >0..15 seconds")
    if args.evidence_output:
        target = args.evidence_output
        if not target.is_absolute() or target.resolve().is_relative_to(PROJECT_ROOT):
            parser.error("evidence output must be an absolute path outside the repository")
    observed_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    watchlist = LoadLongWatchlist(args.watchlist)
    policy = LoadStrategySettings(args.strategy).long_research
    exclusions = historical_exclusions(tuple(args.eligibility_evidence), observed_at)
    reference = ReferencePublication(LoadRuntimeSettings(args.runtime_config).data_paths.reference).read()
    open_dates = reference.open_dates if reference else ()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = tuple(
            pool.map(
                lambda item: audit_code(
                    item.code,
                    observed_at=observed_at,
                    policy=policy,
                    timeout=args.timeout_seconds,
                    exclusions=exclusions,
                    evidence_database=args.company_evidence_database,
                    open_dates=open_dates,
                ),
                watchlist.items,
            )
        )
    counts = Counter(item.disposition for item in results)
    summary = {
        "requested": len(results),
        "retained": counts["retained"],
        "excluded": counts["excluded"],
        "pending": counts["pending"],
        "collected_at": observed_at.isoformat(),
        "evidence_as_of_range": "per-item; see detailed report",
        "coverage": "read-only cached evidence; recent-year scope; full historical risk not certified",
    }
    if args.evidence_output:
        args.evidence_output.write_text(
            json.dumps(
                {
                    "summary": summary,
                    "items": [
                        {
                            "code": item.code,
                            "disposition": item.disposition,
                            "reasons": item.reasons,
                            "evidence_ids": item.evidence_ids,
                            "latest_period": item.latest_period,
                            "annual_periods": item.annual_periods,
                            "evidence_as_of": item.evidence_as_of,
                        }
                        for item in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    emit_report(
        {
            "schema_version": "long-watchlist-admission",
            "summary": summary,
            "status": "degraded" if not results or counts["excluded"] or counts["pending"] else "passed",
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
