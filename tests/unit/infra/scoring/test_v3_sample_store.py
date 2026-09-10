from datetime import date, timedelta
from pathlib import Path

from trader.infra.scoring.profiles.v3.sample_store import V3SampleStore, V3StoredSample


def _sample(code: str, day: date, industry: str = "银行") -> V3StoredSample:
    return V3StoredSample(code, day, "main", industry, 1_000_000.0, (0.1,) * 6, 0.02)


def test_sample_store_streams_by_day_and_industry_in_stable_order(tmp_path: Path) -> None:
    first = date(2026, 1, 2)
    second = first + timedelta(days=1)
    with V3SampleStore(tmp_path / "samples.sqlite3") as store:
        store.add_raw((_sample("600002", first), _sample("600001", first), _sample("600003", second)))

        assert store.raw_dates() == (first, second)
        assert tuple(item.code for item in store.raw_for_date(first)) == ("600001", "600002")

        store.add_final(store.raw_for_date(first))
        store.add_final((_sample("600003", second, "软件"),))

        assert store.count() == 3
        assert store.industries(frozenset((first, second))) == ("软件", "银行")
        assert tuple(item.code for item in store.samples_for("银行", frozenset((first,)))) == (
            "600001",
            "600002",
        )


def test_sample_store_keeps_large_population_out_of_one_python_collection(tmp_path: Path) -> None:
    start = date(2025, 1, 1)
    with V3SampleStore(tmp_path / "samples.sqlite3") as store:
        for offset in range(100):
            day = start + timedelta(days=offset)
            store.add_raw(_sample(f"{code:06d}", day) for code in range(1_000))

        assert store.count() == 0
        assert len(store.raw_for_date(start)) == 1_000
        assert len(store.raw_dates()) == 100
