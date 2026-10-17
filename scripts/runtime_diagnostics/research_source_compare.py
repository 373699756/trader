"""Compare live company-research capabilities without writing caches or runtime state."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import requests

from trader.infra.failures import ClassifyAdapterFailure
from trader.infra.market_data.providers.akshare_http_contracts import AkshareGetFunction, AkshareHttpResponse
from trader.infra.settings import LoadRuntimeSettings, LoadStrategySettings
from trader.recommendation.domain.market.research import ResearchObservation
from trader.recommendation.infra.market_data.announcement_sync import (
    CninfoAnnouncementHttpClient,
    CninfoAnnouncementQuery,
    ParseCninfoAnnouncementRows,
)
from trader.recommendation.infra.market_data.providers.akshare import AkshareResearchClient

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_FIELDS = ("news", "announcements", "regulatory_risk", "financial", "pledge", "unlock")


def _field_coverage(observation: ResearchObservation) -> Mapping[str, bool]:
    return {
        "news": any(item.evidence_type == "news" for item in observation.evidence),
        "announcements": observation.announcements_available,
        "regulatory_risk": observation.corporate_risk_history_complete,
        "financial": observation.financial is not None,
        "pledge": observation.pledge_ratio_pct is not None,
        "unlock": observation.unlock_ratio_pct is not None,
    }


def _probe_eastmoney(
    codes: tuple[str, ...],
    *,
    config: Path,
    timeout_seconds: float,
    loop_seconds: float,
    task_deadline: float,
) -> dict[str, object]:
    runtime = LoadRuntimeSettings(config)
    policy = LoadStrategySettings(runtime.strategy_config_path).long_research
    source_deadline = min(task_deadline, time.monotonic() + loop_seconds)
    samples: list[dict[str, object]] = []
    field_counts = dict.fromkeys(_FIELDS, 0)
    request_count = 0
    started = time.monotonic()
    excluded = False
    for code in codes:
        if time.monotonic() >= source_deadline:
            excluded = True
            break
        code_requests = 0

        def bounded_get(url: str, **kwargs: object) -> AkshareHttpResponse:
            nonlocal code_requests, request_count
            remaining = source_deadline - time.monotonic()
            if remaining <= 0.0 or code_requests >= 20:
                raise requests.Timeout("research_source_diagnostic_budget")
            code_requests += 1
            request_count += 1
            kwargs["timeout"] = min(timeout_seconds, remaining)
            transport = cast(AkshareGetFunction, requests.get)
            return transport(url, **kwargs)

        client = AkshareResearchClient(
            get=bounded_get,
            timeout_seconds=timeout_seconds,
            long_research_policy=policy,
            cancel_requested=lambda: time.monotonic() >= source_deadline,
        )
        sample_started = time.monotonic()
        observation = client.fetch_snapshot(code, observed_at=datetime.now(_SHANGHAI))
        coverage = _field_coverage(observation)
        for field, covered in coverage.items():
            field_counts[field] += int(covered)
        samples.append(
            {
                "code": code,
                "elapsed_ms": round((time.monotonic() - sample_started) * 1000.0, 3),
                "physical_http_requests": code_requests,
                "fields": coverage,
                "missing_fields": [field for field, covered in coverage.items() if not covered],
                "component_errors": list(observation.source_errors),
            }
        )
    elapsed = time.monotonic() - started
    complete_interface = (
        bool(samples) and len(samples) == len(codes) and all(field_counts[field] == len(samples) for field in _FIELDS)
    )
    partial_interface = any(field_counts[field] > 0 for field in _FIELDS)
    return {
        "source": "eastmoney",
        "transport": "existing_research_adapter",
        "tested": True,
        "excluded_by_budget": excluded,
        "elapsed_ms": round(elapsed * 1000.0, 3),
        "physical_http_requests": request_count,
        "sample_count": len(samples),
        "field_coverage": {field: {"covered": field_counts[field], "sample_count": len(samples)} for field in _FIELDS},
        "samples": samples,
        "interface_status": "complete" if complete_interface else "partial" if partial_interface else "failed",
        "production_role": ("candidate_research_primary" if complete_interface and not excluded else "not_eligible"),
    }


def _probe_cninfo(
    codes: tuple[str, ...],
    *,
    timeout_seconds: float,
    loop_seconds: float,
    task_deadline: float,
) -> dict[str, object]:
    source_deadline = min(task_deadline, time.monotonic() + loop_seconds)
    samples: list[dict[str, object]] = []
    request_count = 0
    excluded = False
    started = time.monotonic()
    with requests.Session() as session:

        def counted_post(url: str, **kwargs: Any) -> requests.Response:
            nonlocal request_count
            request_count += 1
            return session.post(url, **kwargs)

        client = CninfoAnnouncementHttpClient(
            post=counted_post,
            timeout_seconds=timeout_seconds,
            wall_clock=lambda: datetime.now(_SHANGHAI),
        )
        for code in codes:
            remaining = source_deadline - time.monotonic()
            if remaining <= 0.0:
                excluded = True
                break
            sample_started = time.monotonic()
            try:
                now = datetime.now(_SHANGHAI)
                payload = client.fetch_announcements(
                    CninfoAnnouncementQuery(
                        code=code,
                        page=1,
                        page_size=30,
                        observed_at=now,
                        cursor_value="",
                        deadline=now + timedelta(seconds=remaining),
                    )
                )
                rows, invalid = ParseCninfoAnnouncementRows(code, payload, now)
                total = payload.get("totalRecordNum")
                has_more = payload.get("hasMore")
                samples.append(
                    {
                        "code": code,
                        "elapsed_ms": round((time.monotonic() - sample_started) * 1000.0, 3),
                        "page_rows": len(rows),
                        "invalid_rows": invalid,
                        "total_records": total if isinstance(total, int) else None,
                        "has_more": has_more if isinstance(has_more, bool) else None,
                        "status": "available",
                    }
                )
            except (OSError, RuntimeError, TypeError, ValueError, requests.RequestException) as exc:
                failure = ClassifyAdapterFailure(exc, provider="cninfo", operation="announcement_page")
                samples.append(
                    {
                        "code": code,
                        "elapsed_ms": round((time.monotonic() - sample_started) * 1000.0, 3),
                        "status": "failed",
                        "error": failure.code.value,
                    }
                )
    available = sum(sample["status"] == "available" for sample in samples)
    return {
        "source": "cninfo",
        "transport": "public_issuer_announcement_query",
        "tested": True,
        "excluded_by_budget": excluded,
        "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
        "physical_http_requests": request_count,
        "sample_count": len(samples),
        "samples": samples,
        "interface_status": "complete" if available == len(codes) else "partial" if available else "failed",
        "production_role": "issuer_announcement_history_supplement",
        "regulatory_history_complete": False,
        "exchange_cross_check_status": "pending",
    }


def Collect(args: argparse.Namespace) -> dict[str, object]:
    task_started = time.monotonic()
    task_deadline = task_started + args.task_seconds
    eastmoney = _probe_eastmoney(
        tuple(args.codes),
        config=args.config,
        timeout_seconds=args.timeout_seconds,
        loop_seconds=args.source_loop_seconds,
        task_deadline=task_deadline,
    )
    cninfo = _probe_cninfo(
        tuple(args.codes),
        timeout_seconds=args.timeout_seconds,
        loop_seconds=args.source_loop_seconds,
        task_deadline=task_deadline,
    )
    sources = [
        {
            "source": "tencent",
            "tested": False,
            "reason": "current_adapter_exposes_quotes_and_kline_only",
            "supported_fields": [],
            "missing_fields": list(_FIELDS),
            "production_role": "unsupported_for_company_research",
        },
        eastmoney,
        cninfo,
    ]
    eastmoney_status = eastmoney["interface_status"]
    eastmoney_usable = eastmoney_status == "complete" and not eastmoney["excluded_by_budget"]
    return {
        "schema_version": "research-source-comparison",
        "status": "passed" if eastmoney_usable else "degraded" if eastmoney_status == "partial" else "failed",
        "observed_at": datetime.now(_SHANGHAI).isoformat(),
        "required_fields": list(_FIELDS),
        "sources": sources,
        "selected_route": {
            "primary": "eastmoney" if eastmoney_usable else None,
            "tencent": "not_a_research_source",
            "schedule": ["09:15", "12:45"],
            "scoring_contract": {
                "news": "evidence_only_no_numeric_weight",
                "official_risk": "existing_level1_level2_filters",
                "financial": "existing_d25_quality_factors",
                "deepseek": "verified_risk_veto_only",
            },
        },
        "budget": {
            "source_loop_limit_seconds": args.source_loop_seconds,
            "task_limit_seconds": args.task_seconds,
            "elapsed_ms": round((time.monotonic() - task_started) * 1000.0, 3),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--codes", nargs="+", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--source-loop-seconds", type=float, default=120.0)
    parser.add_argument("--task-seconds", type=float, default=600.0)
    args = parser.parse_args()
    if any(len(code) != 6 or not code.isdigit() for code in args.codes):
        parser.error("--codes must contain six-digit A-share codes")
    if len(args.codes) > 50:
        parser.error("--codes accepts at most 50 values")
    if not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 30:
        parser.error("--timeout-seconds must be within 0..30")
    if not math.isfinite(args.source_loop_seconds) or not 0 < args.source_loop_seconds <= 120:
        parser.error("--source-loop-seconds must be within 0..120")
    if not math.isfinite(args.task_seconds) or not 0 < args.task_seconds <= 600:
        parser.error("--task-seconds must be within 0..600")
    payload = Collect(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
