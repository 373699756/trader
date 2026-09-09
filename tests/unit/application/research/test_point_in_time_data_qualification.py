from datetime import date

from trader.application.research.baostock_history_runtime import BaoStockRuntimeStatus
from trader.application.research.point_in_time_data_qualification import assemble_point_in_time_data_qualification
from trader.domain.research.h1_point_in_time import H1CapabilityProbe, build_h1_capability_audit
from trader.domain.research.historical_industry_facts import (
    HistoricalIndustrySourceContract,
    build_historical_industry_dataset_report,
    build_historical_industry_source_audit,
    merge_historical_industry_facts,
)


def _industry_report():
    source = build_historical_industry_source_audit(
        HistoricalIndustrySourceContract(
            "baostock_archived_industry",
            "00.9.30",
            True,
            True,
            True,
            True,
            True,
            False,
            True,
        ),
        (),
        (),
    )
    return build_historical_industry_dataset_report("a" * 64, (source,), merge_historical_industry_facts(()))


def test_current_archive_and_free_source_evidence_project_explicit_three_gate_blockers() -> None:
    archive = BaoStockRuntimeStatus(
        state="completed_with_failures",
        sessions=2000,
        shard_count=22,
        universe_count=5453,
        completed_codes=268,
        failed_codes=5185,
        coverage_status="historical_data_insufficient",
        failure_reasons=("incomplete_codes",),
    )
    sources = build_h1_capability_audit(
        (
            H1CapabilityProbe("tencent_qfq_daily", date(2024, 1, 9), False, False, "qfq", False, 640, 1, 1, 1.0),
            H1CapabilityProbe("eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 1, 1, 1.0),
        )
    )

    report = assemble_point_in_time_data_qualification(archive, sources, _industry_report())

    assert report.state == "historical_data_insufficient"
    assert report.daily_archive.completed_codes == 268
    assert report.industry_sources[0].required_sample_codes == 300
    assert "industry_query_time_unavailable" in report.industry_sources[0].failure_reasons
    assert report.minute_sources[0].supports_1450 is False
    assert "minute_raw_qfq_pair_unavailable" in report.minute_sources[0].failure_reasons


def test_daily_archive_count_mismatch_is_reported_instead_of_becoming_an_invalid_report() -> None:
    archive = BaoStockRuntimeStatus(
        state="completed",
        sessions=2000,
        universe_count=5000,
        completed_codes=4999,
        failed_codes=1,
        manifest_hash="a" * 64,
        coverage_status="coverage_ready",
    )
    sources = build_h1_capability_audit(
        (H1CapabilityProbe("eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 1, 1, 1.0),)
    )

    report = assemble_point_in_time_data_qualification(archive, sources, _industry_report())

    assert report.daily_archive.state == "historical_data_insufficient"
    assert "daily_archive_code_coverage_incomplete" in report.daily_archive.failure_reasons
    assert "daily_archive_failed_codes_present" in report.daily_archive.failure_reasons
