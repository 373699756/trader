"""Read-only admission audit; supplier coverage never proves absence of unknown risk."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import NoReturn, cast
from zoneinfo import ZoneInfo

import requests

from .reporting import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.infra.atomic_files.json import RuntimeJsonWriter  # noqa: E402
from trader.recommendation.infra.market_data.providers.akshare import AkshareResearchClient  # noqa: E402
from trader.infra.settings.loading import load_long_watchlist, load_strategy_settings  # noqa: E402
from trader.recommendation.domain.market.research import FinancialReport, LongResearchPolicy  # noqa: E402


@dataclass(frozen=True)
class Admission:
    code: str
    disposition: str
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()
    latest_period: str = ""
    annual_periods: tuple[str, ...] = ()
    evidence_as_of: str = ""


class _ReadOnlyJsonWriter:
    def write(self, _path: Path, _payload: object) -> None:
        raise RuntimeError("read-only diagnostic cannot write evidence cache")


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
    losses = tuple(
        report
        for report in annual
        if any(value is not None and value < 0 for value in (report.parent_net_profit, report.core_net_profit))
    )
    latest_loss = latest is not None and any(
        value is not None and value < 0 for value in (latest.parent_net_profit, latest.core_net_profit)
    )
    reasons: list[str] = []
    evidence = tuple(f"financial:{code}:{report.report_date.isoformat()}" for report in losses)
    if losses:
        reasons.append("historical_annual_loss")
    if latest_loss:
        reasons.append("latest_disclosed_loss")
        assert latest is not None
        evidence += (f"financial:{code}:{latest.report_date.isoformat()}",)
    if reasons:
        disposition = "excluded"
    else:
        years = sorted({report.report_date.year for report in annual})
        if not complete or not annual or not years or years != list(range(years[0], as_of.year)):
            reasons.append("financial_history_incomplete")
        if latest is None or latest.report_date < required_period(as_of):
            reasons.append("latest_report_missing")
        for report in (*annual, *((latest,) if latest else ())):
            if any(
                value is None or not math.isfinite(value)
                for value in (report.parent_net_profit, report.core_net_profit)
            ):
                reasons.append("profit_fields_missing")
                break
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
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            for code, effective_at, evidence_id in connection.execute(
                "SELECT code, effective_at, evidence_id FROM issuer_eligibility_facts"
            ):
                effective = datetime.fromisoformat(effective_at)
                if effective.tzinfo is None:
                    raise ValueError("eligibility evidence time must be timezone-aware")
                if effective <= observed_at:
                    evidence.setdefault(code, []).append(evidence_id)
    return {code: tuple(sorted(set(ids))) for code, ids in evidence.items()}


def cached_observation(
    caches: tuple[Path, ...], source: str, code: str, observed_at: datetime
) -> tuple[datetime, Path] | None:
    observations: list[tuple[datetime, Path]] = []
    for cache in caches:
        path = cache / "raw" / source / f"{code}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            observed = datetime.fromisoformat(record["observed_at"])
            if observed.tzinfo is not None and observed.utcoffset() is not None and observed <= observed_at:
                observations.append((observed, cache))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return max(observations, default=None, key=lambda item: item[0])


def _reject_network(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("read-only archive diagnostic cannot request uncached evidence")


def audit_code(
    code: str,
    *,
    observed_at: datetime,
    policy: LongResearchPolicy,
    timeout: float,
    exclusions: dict[str, tuple[str, ...]],
    evidence_caches: tuple[Path, ...],
) -> Admission:
    if code in exclusions:
        return Admission(code, "excluded", ("existing_permanent_exclusion",), exclusions[code])
    financial_observation = cached_observation(evidence_caches, "financial", code, observed_at)
    announcement_observation = cached_observation(evidence_caches, "announcement", code, observed_at)
    if financial_observation is None:
        return Admission(code, "pending", ("point_in_time_evidence_missing",))
    financial_time, financial_cache = financial_observation
    client = AkshareResearchClient(
        timeout_seconds=timeout,
        get=_reject_network,
        evidence_cache_dir=financial_cache,
        json_writer=cast(RuntimeJsonWriter, _ReadOnlyJsonWriter()),
    )
    try:
        _latest, history, complete, _evidence = client._fetch_financial(code, financial_time, policy)
        admission = financial_admission(code, history, complete, observed_at.date())
        admission = replace(admission, evidence_as_of=financial_time.isoformat())
        if admission.disposition != "financial_pass":
            return admission
    except (requests.RequestException, OSError, RuntimeError, TypeError, ValueError):
        return Admission(code, "pending", ("financial_supplier_evidence_unavailable",))
    if announcement_observation is None:
        return replace(admission, disposition="pending", reasons=("announcement_history_missing",))
    announcement_time, announcement_cache = announcement_observation
    try:
        # Extend the diagnostic window only; production policy is untouched.
        client = AkshareResearchClient(
            timeout_seconds=timeout,
            get=_reject_network,
            evidence_cache_dir=announcement_cache,
            json_writer=cast(RuntimeJsonWriter, _ReadOnlyJsonWriter()),
        )
        announcements, _evidence, _risks, complete, _version = client._fetch_announcements(
            code, announcement_time, replace(policy, announcement_lookback_days=20_000)
        )
        signals = tuple(
            item.announcement_id
            for item in announcements
            if any(
                marker in item.title
                for marker in (
                    "处罚",
                    "立案",
                    "纪律处分",
                    "风险警示",
                    "特别处理",
                    "资金占用",
                    "违规担保",
                    "造假",
                    "虚假记载",
                )
            )
        )
        # Titles are review leads, never newly confirmed permanent facts.
        if signals:
            return replace(
                admission,
                disposition="pending",
                reasons=("regulatory_disclosure_review",),
                evidence_ids=signals,
                evidence_as_of=announcement_time.isoformat(),
            )
        if not complete or not announcements:
            return replace(admission, disposition="pending", reasons=("announcement_history_incomplete",))
        return replace(
            admission,
            disposition="pending",
            reasons=("permanent_eligibility_coverage_unverified",),
            evidence_as_of=min(financial_time, announcement_time).isoformat(),
        )
    except (requests.RequestException, OSError, RuntimeError, TypeError, ValueError):
        return replace(admission, disposition="pending", reasons=("announcement_supplier_evidence_unavailable",))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist", type=Path, default=PROJECT_ROOT / "config/long_watchlist.json")
    parser.add_argument("--strategy", type=Path, default=PROJECT_ROOT / "config/strategy.json")
    parser.add_argument("--eligibility-evidence", type=Path, action="append", default=[])
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--evidence-cache", type=Path, action="append", default=[])
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
    watchlist = load_long_watchlist(args.watchlist)
    policy = load_strategy_settings(args.strategy).long_research
    evidence_caches = tuple(args.evidence_cache) or (
        PROJECT_ROOT / ".runtime/trader/evidence_cache",
        PROJECT_ROOT / ".runtime/v2/evidence_cache",
    )
    exclusions = historical_exclusions(tuple(args.eligibility_evidence), observed_at)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = tuple(
            pool.map(
                lambda item: audit_code(
                    item.code,
                    observed_at=observed_at,
                    policy=policy,
                    timeout=args.timeout_seconds,
                    exclusions=exclusions,
                    evidence_caches=evidence_caches,
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
        "coverage": "read-only cached evidence; full permanent eligibility and current risk not certified",
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
