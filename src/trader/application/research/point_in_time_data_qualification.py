"""Assemble current archive and source evidence into one qualification report."""

from __future__ import annotations

from trader.application.research.baostock_history_runtime import BaoStockRuntimeStatus
from trader.domain.research.h1_point_in_time import H1CapabilityAuditReport, H1CapabilityProbe
from trader.domain.research.historical_effective_facts import baostock_effective_facts_probe
from trader.domain.research.point_in_time_data_qualification import (
    DailyArchiveQualification,
    HistoricalIndustryQualification,
    HistoricalMinuteQualification,
    PointInTimeDataQualificationReport,
    build_point_in_time_data_qualification,
)


def assemble_point_in_time_data_qualification(
    archive: BaoStockRuntimeStatus,
    source_capability: H1CapabilityAuditReport,
) -> PointInTimeDataQualificationReport:
    daily_reasons: list[str] = []
    if archive.sessions != 2000:
        daily_reasons.append("daily_archive_sessions_below_2000")
    if archive.coverage_status != "coverage_ready":
        daily_reasons.append("daily_archive_coverage_incomplete")
    if not archive.manifest_hash:
        daily_reasons.append("daily_archive_manifest_missing")
    if archive.universe_count == 0 or archive.completed_codes != archive.universe_count:
        daily_reasons.append("daily_archive_code_coverage_incomplete")
    if archive.failed_codes:
        daily_reasons.append("daily_archive_failed_codes_present")
    daily = DailyArchiveQualification(
        archive.sessions,
        archive.universe_count,
        archive.completed_codes,
        archive.failed_codes,
        archive.coverage_status,
        archive.manifest_hash,
        "qualified" if not daily_reasons else "historical_data_insufficient",
        tuple(daily_reasons),
    )

    industry_probe = baostock_effective_facts_probe()
    industry_reasons = (
        "industry_sample_below_300",
        "industry_effective_from_unavailable",
        "industry_effective_to_unavailable",
        "industry_query_time_unavailable",
    )
    industry = HistoricalIndustryQualification(
        source="baostock",
        sampled_codes=0,
        required_sample_codes=300,
        code_available=False,
        industry_available=False,
        classification_available=False,
        effective_from_available=industry_probe.industry_effective_at,
        effective_to_available=False,
        queried_at_available=False,
        source_identity_available=True,
        source_evidence_hash=industry_probe.content_hash,
        state="historical_data_insufficient",
        failure_reasons=industry_reasons,
    )

    minute_probes = tuple(
        _minute_qualification(item) for item in source_capability.probes if item.source == "eastmoney_historical_minute"
    )
    if not minute_probes:
        raise ValueError("historical minute capability evidence is missing")
    return build_point_in_time_data_qualification(daily, (industry,), minute_probes)


def _minute_qualification(probe: H1CapabilityProbe) -> HistoricalMinuteQualification:
    matched = int(probe.earliest_available is not None and probe.page_size > 0)
    reasons = tuple(
        reason
        for reason, missing in (
            ("historical_1120_anchor_unavailable", not probe.supports_today_1120),
            ("historical_1450_anchor_unavailable", not probe.supports_1450),
            ("minute_amount_unavailable", True),
            ("minute_company_action_semantics_unproven", True),
            ("minute_coverage_below_95_percent", matched == 0),
            ("minute_raw_qfq_pair_unavailable", True),
            ("minute_timezone_unproven", True),
            ("minute_volume_unavailable", True),
        )
        if missing
    )
    return HistoricalMinuteQualification(
        probe.source,
        probe.earliest_available,
        1,
        matched,
        1,
        matched,
        float(matched),
        "",
        probe.supports_today_1120,
        probe.supports_1450,
        False,
        False,
        False,
        False,
        probe.content_hash,
        "historical_data_insufficient",
        reasons or ("historical_minute_capability_unproven",),
    )


__all__ = ["assemble_point_in_time_data_qualification"]
