from datetime import date

from trader.application.research.history_archive_status import HistoryArchiveStatus
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
    archive = HistoryArchiveStatus(
        state="invalid",
        active_snapshot_hash="a" * 64,
        data_cutoff=date(2026, 9, 10),
        label_cutoff=date(2026, 9, 9),
        calendar_sessions=2000,
        universe_count=5453,
        partition_count=100,
        reason="history_snapshot_partition_invalid",
    )
    sources = build_h1_capability_audit(
        (
            H1CapabilityProbe("tencent_qfq_daily", date(2024, 1, 9), False, False, "qfq", False, 640, 1, 1, 1.0),
            H1CapabilityProbe("eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 1, 1, 1.0),
        )
    )

    report = assemble_point_in_time_data_qualification(archive, sources, _industry_report())

    assert report.state == "historical_data_insufficient"
    assert report.daily_archive.completed_codes == 0
    assert report.industry_sources[0].required_sample_codes == 300
    assert "industry_query_time_unavailable" in report.industry_sources[0].failure_reasons
    assert report.minute_sources[0].supports_1450 is False
    assert "minute_raw_qfq_pair_unavailable" in report.minute_sources[0].failure_reasons


def test_daily_archive_count_mismatch_is_reported_instead_of_becoming_an_invalid_report() -> None:
    archive = HistoryArchiveStatus(
        state="active",
        active_snapshot_hash="a" * 64,
        data_cutoff=date(2026, 9, 10),
        label_cutoff=date(2026, 9, 9),
        calendar_sessions=1999,
        universe_count=5000,
        partition_count=100,
        reason=None,
    )
    sources = build_h1_capability_audit(
        (H1CapabilityProbe("eastmoney_historical_minute", None, False, False, "unsupported", False, 0, 1, 1, 1.0),)
    )

    report = assemble_point_in_time_data_qualification(archive, sources, _industry_report())

    assert report.daily_archive.state == "historical_data_insufficient"
    assert "daily_archive_sessions_below_2000" in report.daily_archive.failure_reasons
    assert report.daily_archive.completed_codes == 5000
    assert report.daily_archive.failed_codes == 0
