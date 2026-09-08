from datetime import date

from trader.domain.research.point_in_time_data_qualification import (
    DailyArchiveQualification,
    HistoricalIndustryQualification,
    HistoricalMinuteQualification,
    build_point_in_time_data_qualification,
)


def test_three_independent_data_gates_fail_closed_without_opening_holdout() -> None:
    daily = DailyArchiveQualification(
        2000,
        5453,
        268,
        5185,
        "historical_data_insufficient",
        "",
        "historical_data_insufficient",
        ("daily_archive_manifest_missing",),
    )
    industry = HistoricalIndustryQualification(
        "baostock",
        0,
        300,
        True,
        True,
        True,
        False,
        False,
        False,
        True,
        "a" * 64,
        "historical_data_insufficient",
        ("industry_effective_from_unavailable",),
    )
    minute = HistoricalMinuteQualification(
        "eastmoney_historical_minute",
        None,
        1,
        0,
        1,
        0,
        0.0,
        "",
        False,
        False,
        False,
        False,
        False,
        False,
        "b" * 64,
        "historical_data_insufficient",
        ("historical_1450_anchor_unavailable",),
    )

    report = build_point_in_time_data_qualification(daily, (industry,), (minute,))

    assert report.state == "historical_data_insufficient"
    assert report.failure_reasons == (
        "daily_archive_not_qualified",
        "historical_industry_not_qualified",
        "historical_minute_not_qualified",
    )
    assert report.point_in_time_parity is False
    assert report.terminal_holdout_opened is False
    assert report.production_authority is False
    assert len(report.content_hash) == 64


def test_report_qualifies_only_when_daily_industry_and_minute_gates_all_pass() -> None:
    daily = DailyArchiveQualification(2000, 5000, 5000, 0, "coverage_ready", "a" * 64, "qualified", ())
    industry = HistoricalIndustryQualification(
        "qualified_industry",
        500,
        300,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        "b" * 64,
        "qualified",
        (),
    )
    minute = HistoricalMinuteQualification(
        "qualified_minute",
        date(2018, 1, 2),
        9,
        9,
        27,
        27,
        1.0,
        "Asia/Shanghai",
        True,
        True,
        True,
        True,
        True,
        True,
        "c" * 64,
        "qualified",
        (),
    )

    report = build_point_in_time_data_qualification(daily, (industry,), (minute,))

    assert report.state == "qualified"
    assert report.failure_reasons == ()
    assert report.point_in_time_parity is True
