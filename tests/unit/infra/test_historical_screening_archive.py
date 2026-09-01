from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.application.research.historical_screening import HistoricalSecurity
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalPriceBar
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

    archive.register_universe(SCORE_H0_V1_SPEC, (security,))
    archive.register_universe(SCORE_H0_V1_SPEC, (security,))
    assert archive.registered_universe(SCORE_H0_V1_SPEC.research_identity) == (security,)
    archive.save_history(SCORE_H0_V1_SPEC, security.code, (_bar(),))
    archive.save_history(SCORE_H0_V1_SPEC, security.code, (_bar(),))

    assert archive.completed_codes(SCORE_H0_V1_SPEC.research_identity) == frozenset({"600001"})
    status = archive.inspect(SCORE_H0_V1_SPEC.research_identity)
    assert status.universe_count == 1
    assert status.completed_codes == 1
    assert status.bar_count == 1
    assert status.first_trade_date == "2026-08-19"
    assert status.last_trade_date == "2026-08-19"

    manifest = archive.manifest(SCORE_H0_V1_SPEC)
    assert manifest.spec_hash == SCORE_H0_V1_SPEC.content_hash
    assert len(manifest.universe_hash) == 64
    assert len(manifest.histories_hash) == 64
    assert manifest.histories[0].code == "600001"
    assert manifest.histories[0].bar_count == 1
    assert len(manifest.content_hash) == 64

    with pytest.raises(HistoricalArchiveConflictError):
        archive.save_history(SCORE_H0_V1_SPEC, security.code, (replace(_bar(), close=10.6, high=10.8),))

    with pytest.raises(HistoricalArchiveConflictError, match="universe set"):
        archive.register_universe(
            SCORE_H0_V1_SPEC,
            (security, HistoricalSecurity("600002", "main", "乙", False, False)),
        )


def test_history_archive_manifest_detects_bar_payload_tampering(tmp_path) -> None:
    import sqlite3

    archive = SQLiteHistoricalArchive(tmp_path)
    security = HistoricalSecurity("600001", "main", "甲", False, False)
    archive.register_universe(SCORE_H0_V1_SPEC, (security,))
    archive.save_history(SCORE_H0_V1_SPEC, security.code, (_bar(),))

    database = tmp_path / "score-history" / "score-history.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE bars SET close_price = 10.7 WHERE code = '600001'")

    with pytest.raises(HistoricalArchiveConflictError, match="bar payload"):
        archive.manifest(SCORE_H0_V1_SPEC)


def test_history_archive_status_is_read_only_when_database_does_not_exist(tmp_path) -> None:
    archive = SQLiteHistoricalArchive(tmp_path)

    status = archive.inspect(SCORE_H0_V1_SPEC.research_identity)

    assert status.initialized is False
    assert not (tmp_path / "score-history").exists()


def test_history_archive_screening_uses_only_rows_with_61_inputs_and_5_future_labels(tmp_path) -> None:
    archive = SQLiteHistoricalArchive(tmp_path)
    start = date(2024, 8, 1)
    securities = tuple(
        HistoricalSecurity(f"60{index:04d}", "main", f"样本{index}", False, False) for index in range(30)
    )
    archive.register_universe(SCORE_H0_V1_SPEC, securities)
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
        archive.save_history(SCORE_H0_V1_SPEC, security.code, tuple(bars))

    days = archive.screening_days(SCORE_H0_V1_SPEC)
    r6_rows = archive.score_r6_rows(SCORE_H0_V1_SPEC)
    daily_rows = archive.score_r6_daily_rows(SCORE_H0_V1_SPEC)
    p2_rows = archive.tomorrow_historical_p2_rows(SCORE_H0_V1_SPEC)
    risk_rows = archive.tomorrow_historical_risk_rows(SCORE_H0_V1_SPEC)

    assert len(days) == 1
    assert days[0].trade_date == start + timedelta(days=60)
    assert days[0].population == 30
    assert days[0].selected == 3
    assert len(r6_rows) == 30
    assert {row.trade_date for row in r6_rows} == {start + timedelta(days=60)}
    assert {row.board for row in r6_rows} == {"main"}
    assert all(row.volatility_20d_pct < 4.0 for row in r6_rows)
    assert len(daily_rows) == 30
    assert {row.trade_date for row in daily_rows} == {start + timedelta(days=60)}
    assert all(row.residual_return_60_5_pct > 0.0 for row in daily_rows)
    assert all(row.close_ma20_spread_pct > 0.0 for row in daily_rows)
    assert all(row.drawdown_60d_pct < 0.0 for row in daily_rows)
    assert all(row.downside_volatility_20d_pct == 0.0 for row in daily_rows)
    assert [row.trend_efficiency_score for row in daily_rows] == sorted(
        row.trend_efficiency_score for row in daily_rows
    )
    assert len(risk_rows) == len(p2_rows) == 150
    assert all(risk.atr20_pct > 0.0 for risk in risk_rows)
    assert all(risk.gross_return - risk.benchmark_return == risk.gross_excess_return for risk in risk_rows)
    assert not hasattr(p2_rows[0], "atr20_pct")
