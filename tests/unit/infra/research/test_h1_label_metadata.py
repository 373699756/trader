from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.application.research.historical_screening import HistoricalSecurity
from trader.domain.research.h1_point_in_time import H1PointInTimeRecord, H1PointInTimeSpec
from trader.domain.research.historical_screening import HistoricalPriceBar
from trader.infra.research.h1_point_in_time_archive import SQLiteH1PointInTimeArchive


def _record(code: str, day: date) -> H1PointInTimeRecord:
    bar = HistoricalPriceBar(day, 10.0, 10.2, 10.3, 9.9, 100.0, 1000.0, 2.0, None, "qfq", "fixture")
    digest = "a" * 64
    return H1PointInTimeRecord(
        "today",
        code,
        day,
        datetime.combine(day, datetime.min.time(), tzinfo=ZoneInfo("Asia/Shanghai")).replace(hour=11, minute=20),
        bar,
        10.1,
        50.0,
        500.0,
        digest,
        digest,
        digest,
    )


def test_h1_label_metadata_reads_only_common_dates_and_manifest_identity(tmp_path) -> None:
    archive = SQLiteH1PointInTimeArchive(tmp_path)
    spec = H1PointInTimeSpec("today")
    securities = (
        HistoricalSecurity("600001", "main", "A", False, False),
        HistoricalSecurity("600002", "main", "B", False, False),
    )
    archive.register_universe(spec, securities)
    first = date(2024, 1, 1)
    archive.save_records(spec, "600001", tuple(_record("600001", first + timedelta(days=i)) for i in range(3)))
    archive.save_records(spec, "600002", tuple(_record("600002", first + timedelta(days=i)) for i in range(1, 4)))

    metadata = archive.label_metadata(spec)

    assert metadata.common_trading_dates == (first + timedelta(days=1), first + timedelta(days=2))
    assert metadata.universe_hash == archive.manifest(spec).universe_hash
    assert metadata.h1_manifest_hash == archive.manifest(spec).content_hash
    assert metadata.source_cutoff == date(2026, 8, 31)
    assert not hasattr(metadata, "returns")
