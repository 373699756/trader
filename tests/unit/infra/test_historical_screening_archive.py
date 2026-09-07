from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.application.research.historical_screening import HistoricalSecurity
from trader.domain.research.historical_screening import HISTORICAL_SCREENING_SPEC, HistoricalPriceBar
from trader.infra.research.history_archive import HistoricalArchiveConflictError, SQLiteHistoricalArchive


def _bar(close: float = 10.5) -> HistoricalPriceBar:
    return HistoricalPriceBar(
        trade_date=__import__("datetime").date(2026, 8, 19),
        open_price=10.0,
        close=close,
        high=max(10.8, close),
        low=9.9,
        volume=1000.0,
        amount=10000.0,
        pct_change=5.0,
        turnover_rate=None,
        adjustment="qfq",
        source="tencent",
    )


def test_history_archive_is_idempotent_and_rejects_same_identity_conflicts(tmp_path) -> None:
    archive = SQLiteHistoricalArchive(tmp_path)
    security = HistoricalSecurity("600001", "main", "甲", False, False)

    archive.register_universe(HISTORICAL_SCREENING_SPEC, (security,))
    archive.register_universe(HISTORICAL_SCREENING_SPEC, (security,))
    assert archive.registered_universe(HISTORICAL_SCREENING_SPEC.research_identity) == (security,)
    archive.save_history(HISTORICAL_SCREENING_SPEC, security.code, (_bar(),))
    archive.save_history(HISTORICAL_SCREENING_SPEC, security.code, (_bar(),))

    assert archive.completed_codes(HISTORICAL_SCREENING_SPEC.research_identity) == frozenset({"600001"})
    status = archive.inspect(HISTORICAL_SCREENING_SPEC.research_identity)
    assert status.universe_count == 1
    assert status.completed_codes == 1
    assert status.bar_count == 1
    assert status.first_trade_date == "2026-08-19"
    assert status.last_trade_date == "2026-08-19"

    manifest = archive.manifest(HISTORICAL_SCREENING_SPEC)
    assert manifest.spec_hash == HISTORICAL_SCREENING_SPEC.content_hash
    assert len(manifest.universe_hash) == 64
    assert len(manifest.histories_hash) == 64
    assert manifest.histories[0].code == "600001"
    assert manifest.histories[0].bar_count == 1
    assert len(manifest.content_hash) == 64

    with pytest.raises(HistoricalArchiveConflictError):
        archive.save_history(HISTORICAL_SCREENING_SPEC, security.code, (replace(_bar(), close=10.6, high=10.8),))

    with pytest.raises(HistoricalArchiveConflictError, match="universe set"):
        archive.register_universe(
            HISTORICAL_SCREENING_SPEC,
            (security, HistoricalSecurity("600002", "main", "乙", False, False)),
        )


def test_history_archive_manifest_detects_bar_payload_tampering(tmp_path) -> None:
    import sqlite3

    archive = SQLiteHistoricalArchive(tmp_path)
    security = HistoricalSecurity("600001", "main", "甲", False, False)
    archive.register_universe(HISTORICAL_SCREENING_SPEC, (security,))
    archive.save_history(HISTORICAL_SCREENING_SPEC, security.code, (_bar(),))

    database = tmp_path / "score-history" / "score-history.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE bars SET close_price = 10.7 WHERE code = '600001'")

    with pytest.raises(HistoricalArchiveConflictError, match="bar payload"):
        archive.manifest(HISTORICAL_SCREENING_SPEC)


def test_history_archive_status_is_read_only_when_database_does_not_exist(tmp_path) -> None:
    archive = SQLiteHistoricalArchive(tmp_path)

    status = archive.inspect(HISTORICAL_SCREENING_SPEC.research_identity)

    assert status.initialized is False
    assert not (tmp_path / "score-history").exists()


def test_history_archive_screening_uses_only_rows_with_61_inputs_and_5_future_labels(tmp_path) -> None:
    archive = SQLiteHistoricalArchive(tmp_path)
    start = date(2024, 8, 1)
    securities = tuple(
        HistoricalSecurity(f"60{index:04d}", "main", f"样本{index}", False, False) for index in range(30)
    )
    archive.register_universe(HISTORICAL_SCREENING_SPEC, securities)
    for index, security in enumerate(securities):
        bars = []
        for offset in range(66):
            growth = 0.0005 * (index + 1)
            close = (10.0 + index) * (1.0 + growth) ** offset
            bars.append(
                HistoricalPriceBar(
                    trade_date=start + timedelta(days=offset),
                    open_price=close,
                    close=close,
                    high=close * 1.01,
                    low=close * 0.99,
                    volume=1000.0,
                    amount=float((index + 1) * 1_000_000),
                    pct_change=100.0 if offset == 0 and index % 2 == 0 else growth * 100.0,
                    turnover_rate=None,
                    adjustment="qfq",
                    source="fixture",
                )
            )
        archive.save_history(HISTORICAL_SCREENING_SPEC, security.code, tuple(bars))

    days = archive.screening_days(HISTORICAL_SCREENING_SPEC)
    historical_rows = archive.tomorrow_historical_rows(HISTORICAL_SCREENING_SPEC)
    risk_rows = archive.tomorrow_historical_risk_rows(HISTORICAL_SCREENING_SPEC)

    assert len(days) == 1
    assert days[0].trade_date == start + timedelta(days=60)
    assert days[0].population == 30
    assert days[0].selected == 3
    assert len(risk_rows) == len(historical_rows) == 150
    assert all(risk.atr20_pct > 0.0 for risk in risk_rows)
    assert all(risk.gross_return - risk.benchmark_return == risk.gross_excess_return for risk in risk_rows)
    assert not hasattr(historical_rows[0], "atr20_pct")
