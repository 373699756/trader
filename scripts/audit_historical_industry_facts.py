#!/usr/bin/env python3
"""Audit historical industry evidence without modifying the daily archive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader.domain.research.historical_industry_facts import (  # noqa: E402
    HistoricalIndustryDatasetReport,
    HistoricalIndustrySourceAudit,
)
from trader.infra.research.historical_industry_archive import (  # noqa: E402
    audit_archived_historical_industry_facts,
)


def execute(
    *,
    history_root: Path,
    required_sample_codes: int,
    tushare_access_points: int,
) -> HistoricalIndustryDatasetReport:
    return audit_archived_historical_industry_facts(
        history_root,
        required_sample_codes=required_sample_codes,
        tushare_access_points=tushare_access_points,
    )


def project_historical_industry_report(
    report: HistoricalIndustryDatasetReport,
    *,
    include_details: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": report.schema_version,
        "status": report.status,
        "daily_manifest_hash": report.daily_manifest_hash,
        "dataset_hash": report.dataset_hash,
        "content_hash": report.content_hash,
        "eligible_codes": len(report.eligible_codes),
        "merged_fact_hash_count": len(report.merged_fact_hashes),
        "cross_source_conflicts": report.cross_source_conflicts,
        "failure_reasons": list(report.failure_reasons),
        "training_authority": report.training_authority,
        "production_authority": report.production_authority,
        "sources": [_project_source(item, include_details=include_details) for item in report.sources],
    }
    if include_details:
        payload["merged_fact_hashes"] = list(report.merged_fact_hashes)
    return payload


def _project_source(source: HistoricalIndustrySourceAudit, *, include_details: bool) -> dict[str, object]:
    contract = source.contract
    payload: dict[str, object] = {
        "source": source.source,
        "source_version": source.source_version,
        "status": source.status,
        "sampled_codes": source.sampled_codes,
        "required_sample_codes": source.required_sample_codes,
        "eligible_codes": len(source.eligible_codes),
        "actual_trading_dates": source.actual_trading_dates,
        "covered_trading_dates": source.covered_trading_dates,
        "coverage_ratio": source.coverage_ratio,
        "complete_codes": source.complete_codes,
        "incomplete_codes": source.incomplete_codes,
        "missing_dates": source.missing_dates,
        "conflict_dates": source.conflict_dates,
        "time_travel_dates": source.time_travel_dates,
        "board_counts": dict(source.board_counts),
        "cohort_counts": dict(source.cohort_counts),
        "code_available": contract.code_available,
        "industry_available": contract.industry_available,
        "classification_available": contract.classification_available,
        "effective_from_available": contract.effective_from_available,
        "effective_to_available": contract.effective_to_available,
        "queried_at_available": contract.queried_at_available,
        "source_identity_available": contract.source_identity_available,
        "fact_hash_count": len(source.fact_hashes),
        "dataset_hash": source.dataset_hash,
        "failure_reasons": list(source.failure_reasons),
    }
    if include_details:
        payload["fact_hashes"] = list(source.fact_hashes)
        payload["stocks"] = [
            {
                "code": item.code,
                "board": item.board,
                "cohort": item.cohort,
                "actual_trading_dates": item.actual_trading_dates,
                "covered_trading_dates": item.covered_trading_dates,
                "missing_dates": item.missing_dates,
                "conflict_dates": item.conflict_dates,
                "time_travel_dates": item.time_travel_dates,
                "eligible": item.eligible,
                "failure_reasons": list(item.failure_reasons),
            }
            for item in source.stocks
        ]
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, default=PROJECT_ROOT / "data" / "history")
    parser.add_argument("--required-sample-codes", type=int, default=300)
    parser.add_argument("--tushare-access-points", type=int, default=0)
    parser.add_argument("--include-details", action="store_true")
    parser.add_argument("--output", default="-", help="- for bounded stdout or an absolute external path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output: Path | None = None
    try:
        output = _output_path(args.output, include_details=args.include_details)
        report = execute(
            history_root=args.history_root.expanduser().resolve(),
            required_sample_codes=args.required_sample_codes,
            tushare_access_points=args.tushare_access_points,
        )
        payload = project_historical_industry_report(report, include_details=args.include_details)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        payload = {
            "schema_version": "historical_industry_dataset",
            "status": "audit_failed",
            "error_code": _error_code(exc),
            "training_authority": False,
            "production_authority": False,
        }
        _write(payload, output)
        return 2
    _write(payload, output)
    return 0 if report.status == "qualified" else 1


def _output_path(value: str, *, include_details: bool) -> Path | None:
    if value == "-":
        if include_details:
            raise ValueError("--include-details requires an external --output path")
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("--output must be - or an absolute repository-external path")
    resolved = path.resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError("--output must be outside the repository")
    return resolved


def _write(payload: dict[str, object], output: Path | None) -> None:
    document = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if output is None:
        print(document, flush=True)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document + "\n", encoding="utf-8")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "artifact_io_failed"
    if "manifest" in str(exc).lower() or "partition" in str(exc).lower():
        return "daily_archive_invalid"
    return "invalid_industry_evidence"


if __name__ == "__main__":
    raise SystemExit(main())
