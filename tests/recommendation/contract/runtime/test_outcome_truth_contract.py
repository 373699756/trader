from __future__ import annotations

import ast
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide
from trader.recommendation.domain.market.history_metrics import DailyBar, PriceAdjustment
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.outcomes.models import OutcomeBar, OutcomeHorizons, OutcomePrice, OutcomeTradingStatus
from trader.recommendation.infra.market_data import outcome_history, published_history_bars

ROOT = Path(__file__).resolve().parents[4]
DOMAIN = ROOT / "src/trader/recommendation/domain/outcomes/evaluation.py"
MODELS = ROOT / "src/trader/recommendation/domain/outcomes/models.py"
APPLICATION = ROOT / "src/trader/recommendation/application/outcome_settlement.py"
PERSISTENCE = ROOT / "src/trader/recommendation/infra/persistence/outcome_evidence_index.py"
HISTORY = ROOT / "src/trader/recommendation/infra/market_data/published_history_cache.py"
WORK = ROOT / "docs/g_施工进度.md"


def test_d25_horizons_have_one_domain_owner_and_include_t4() -> None:
    application = APPLICATION.read_text(encoding="utf-8")
    persistence = PERSISTENCE.read_text(encoding="utf-8")

    assert OutcomeHorizons(Strategy.D25) == (2, 3, 4, 5)
    assert "OutcomeHorizons(target.strategy)" in application
    assert "OutcomeHorizons(strategy)" in persistence
    assert "(2, 3, 5)" not in application
    assert "(2, 3, 5)" not in persistence


def test_canonical_evaluator_is_pure_domain_code() -> None:
    tree = ast.parse(DOMAIN.read_text(encoding="utf-8"), filename=str(DOMAIN))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }

    assert "class CanonicalOutcomeEvaluator" in DOMAIN.read_text(encoding="utf-8")
    assert not any(
        name.startswith(("trader.application", "trader.infra", "trader.web", "flask", "requests")) for name in imports
    )


def test_persistence_returns_only_missing_horizons_for_restart_recovery() -> None:
    persistence = PERSISTENCE.read_text(encoding="utf-8")

    assert "pending_horizons = self._pending_horizons" in persistence
    assert "pending_horizons," in persistence
    assert "tuple(horizon for horizon in horizons if horizon not in settled)" in persistence


def test_outcome_contract_owns_explicit_price_basis_and_exit_tradability() -> None:
    models = MODELS.read_text(encoding="utf-8")
    domain = DOMAIN.read_text(encoding="utf-8")
    application = APPLICATION.read_text(encoding="utf-8")
    persistence = PERSISTENCE.read_text(encoding="utf-8")
    history = HISTORY.read_text(encoding="utf-8")

    assert "class OutcomePrice" in models
    assert "qfq: OutcomePrice" in models
    assert "raw: OutcomePrice" in models
    assert "anchor_raw_price" in models
    assert "anchor_qfq_price" in models
    assert "exit_status" in models
    assert "missing_carried_forward" in models
    assert "_anchor_qfq_price" in domain
    assert "_settlement_window" in domain
    assert "expected_trade_dates=expected_dates" in application
    assert '"exit_untradable": outcome.exit_untradable' in persistence
    assert "read_windows" in history
    assert "OutcomeBars(window.cells)" in history


def test_online_and_published_outcomes_share_builder_without_replacing_missing_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, OutcomePrice, OutcomePrice, float, float | None, str]] = []
    builder = outcome_history.BuildOutcomeBar

    def spy(
        *,
        trade_date: str,
        qfq: OutcomePrice,
        raw: OutcomePrice,
        volume: float,
        pct_change: float | None,
        source: str,
    ) -> OutcomeBar:
        calls.append((trade_date, qfq, raw, volume, pct_change, source))
        return builder(
            trade_date=trade_date,
            qfq=qfq,
            raw=raw,
            volume=volume,
            pct_change=pct_change,
            source=source,
        )

    monkeypatch.setattr(outcome_history, "BuildOutcomeBar", spy)
    monkeypatch.setattr(published_history_bars, "BuildOutcomeBar", spy)
    raw_bar = DailyBar(
        trade_date="2026-11-06",
        open_price=9.0,
        close=9.0,
        high=9.0,
        low=9.0,
        volume=1000.0,
        amount=9000.0,
        pct_change=-10.0,
        adjustment=PriceAdjustment.RAW,
        source="fixture",
    )
    qfq_bar = replace(
        raw_bar,
        open_price=4.5,
        close=4.5,
        high=4.5,
        low=4.5,
        pct_change=0.0,
        adjustment=PriceAdjustment.QFQ,
    )
    raw_side = PublishedHistorySide(
        code="600001",
        trade_date=date(2026, 11, 6),
        adjustment="unadjusted",
        open_price=9.0,
        high_price=9.0,
        low_price=9.0,
        close_price=9.0,
        volume=1000.0,
        amount=9000.0,
        preclose=None,
        pct_change=None,
        turnover=0.5,
        trading_status="trading",
    )
    qfq_side = replace(
        raw_side,
        adjustment="qfq",
        open_price=4.5,
        high_price=4.5,
        low_price=4.5,
        close_price=4.5,
        amount=4500.0,
        turnover=None,
    )
    cell = PublishedHistoryCell(
        code="600001",
        trade_date=raw_side.trade_date,
        status="complete",
        unadjusted=raw_side,
        qfq=qfq_side,
    )

    online = outcome_history.PairOutcomeHistory((qfq_bar,), (raw_bar,))
    published = published_history_bars.OutcomeBars((cell,))

    qfq_prices = OutcomePrice(4.5, 4.5, 4.5, 4.5)
    raw_prices = OutcomePrice(9.0, 9.0, 9.0, 9.0)
    assert calls == [
        ("2026-11-06", qfq_prices, raw_prices, 1000.0, -10.0, "fixture"),
        ("2026-11-06", qfq_prices, raw_prices, 1000.0, None, "baostock"),
    ]
    assert online[0].trading_status is OutcomeTradingStatus.ONE_PRICE_LIMIT_DOWN
    assert published[0].trading_status is OutcomeTradingStatus.UNKNOWN
