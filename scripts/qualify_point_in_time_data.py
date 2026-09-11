#!/usr/bin/env python3
"""Audit archive, industry, and one-code historical anchors without building an archive."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import cast

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader.application.research.point_in_time_data_qualification import (  # noqa: E402
    assemble_point_in_time_data_qualification,
)
from trader.domain.research.point_in_time_data_qualification import (  # noqa: E402
    PointInTimeDataQualificationReport,
)
from trader.infra.research.h1_point_in_time_capability import (  # noqa: E402
    FreeSourceH1CapabilityProbe,
    PointInTimeSourceSession,
)
from trader.infra.research.historical_industry_archive import (  # noqa: E402
    audit_archived_historical_industry_facts,
)
from trader.infra.research.history_archive_status import inspect_history_archive  # noqa: E402


def execute(
    *,
    history_root: Path,
    code: str,
    historical_anchor_date: date,
    timeout_seconds: float,
    session: PointInTimeSourceSession,
) -> PointInTimeDataQualificationReport:
    archive = inspect_history_archive(history_root, verify_partitions=True)
    industry = audit_archived_historical_industry_facts(history_root)
    sources = FreeSourceH1CapabilityProbe(session, timeout_seconds=timeout_seconds).run(
        code=code,
        historical_anchor_date=historical_anchor_date,
    )
    return assemble_point_in_time_data_qualification(archive, sources, industry)


def project_point_in_time_data_qualification(
    report: PointInTimeDataQualificationReport,
) -> dict[str, object]:
    daily = report.daily_archive
    return {
        "schema_version": report.schema_version,
        "status": report.state,
        "content_hash": report.content_hash,
        "failure_reasons": list(report.failure_reasons),
        "daily_archive": {
            "status": daily.state,
            "sessions": daily.sessions,
            "universe_count": daily.universe_count,
            "completed_codes": daily.completed_codes,
            "failed_codes": daily.failed_codes,
            "coverage_status": daily.coverage_status,
            "manifest_hash": daily.manifest_hash,
            "failure_reasons": list(daily.failure_reasons),
        },
        "industry_sources": [
            {
                "source": item.source,
                "status": item.state,
                "sampled_codes": item.sampled_codes,
                "required_sample_codes": item.required_sample_codes,
                "code_available": item.code_available,
                "industry_available": item.industry_available,
                "classification_available": item.classification_available,
                "effective_from_available": item.effective_from_available,
                "effective_to_available": item.effective_to_available,
                "queried_at_available": item.queried_at_available,
                "source_identity_available": item.source_identity_available,
                "source_evidence_hash": item.source_evidence_hash,
                "failure_reasons": list(item.failure_reasons),
            }
            for item in report.industry_sources
        ],
        "minute_sources": [
            {
                "source": item.source,
                "status": item.state,
                "earliest_available": item.earliest_available.isoformat()
                if item.earliest_available is not None
                else None,
                "sampled_codes": item.sampled_codes,
                "matched_codes": item.matched_codes,
                "sampled_trade_dates": item.sampled_trade_dates,
                "matched_trade_dates": item.matched_trade_dates,
                "coverage_ratio": item.coverage_ratio,
                "timezone": item.timezone,
                "supports_1120": item.supports_1120,
                "supports_1450": item.supports_1450,
                "volume_available": item.volume_available,
                "amount_available": item.amount_available,
                "raw_qfq_pair_available": item.raw_qfq_pair_available,
                "corporate_action_semantics_proven": item.corporate_action_semantics_proven,
                "source_evidence_hash": item.source_evidence_hash,
                "failure_reasons": list(item.failure_reasons),
            }
            for item in report.minute_sources
        ],
        "point_in_time_parity": report.point_in_time_parity,
        "terminal_holdout_opened": report.terminal_holdout_opened,
        "production_authority": report.production_authority,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, default=PROJECT_ROOT / "data" / "history")
    parser.add_argument("--code", default="600519", help="representative code; never included in output")
    parser.add_argument("--historical-anchor-date", type=date.fromisoformat, default=date(2022, 1, 4))
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--output", default="-", help="- for stdout or an absolute repository-external path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output: Path | None = None
    try:
        output = _output_path(args.output)
        with requests.Session() as session:
            session.trust_env = False
            session.headers.update({"User-Agent": "Mozilla/5.0"})
            report = execute(
                history_root=args.history_root.expanduser().resolve(),
                code=args.code,
                historical_anchor_date=args.historical_anchor_date,
                timeout_seconds=args.timeout_seconds,
                session=cast(PointInTimeSourceSession, session),
            )
        payload = project_point_in_time_data_qualification(report)
    except (OSError, RuntimeError, TypeError, ValueError, requests.RequestException) as exc:
        payload = {
            "schema_version": "point_in_time_data_qualification",
            "status": "probe_failed",
            "error_code": _error_code(exc),
            "production_authority": False,
        }
        _write(payload, output)
        return 2
    _write(payload, output)
    return 0 if report.state == "qualified" else 1


def _output_path(value: str) -> Path | None:
    if value == "-":
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("--output must be - or an absolute repository-external path")
    resolved = path.resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError("--output must be outside the repository")
    return resolved


def _write(payload: dict[str, object], output: Path | None) -> None:
    document = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if output is None:
        print(document, flush=True)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document + "\n", encoding="utf-8")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, requests.Timeout):
        return "source_timeout"
    if isinstance(exc, requests.RequestException):
        return "source_request_failed"
    if isinstance(exc, OSError):
        return "artifact_io_failed"
    return "invalid_qualification_evidence"


if __name__ == "__main__":
    raise SystemExit(main())
