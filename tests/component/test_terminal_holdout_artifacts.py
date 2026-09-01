from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.research.terminal_holdout import TerminalHoldoutEvaluation, TerminalHoldoutRow, evaluate_terminal_holdout
from trader.infra.research.terminal_holdout_artifacts import (
    TerminalHoldoutArtifactConflictError,
    TerminalHoldoutArtifactStore,
)


def _report():
    rows = tuple(
        TerminalHoldoutRow(
            trade_date=date(2025, 1, 1) + timedelta(days=day),
            code=f"60{stock:04d}",
            board=("main", "chinext", "star")[stock % 3],
            industry=f"industry-{stock % 10}",
            market_state=("up", "down", "sideways")[day % 3],
            volatility_state=("low", "high")[day % 2],
            liquidity_state=("high", "low")[day % 2],
            predicted_net_excess_return=0.01 + stock / 10000,
            actual_net_excess_returns=(0.01, 0.007, 0.002),
            baseline_net_excess_returns=(0.0, -0.003, -0.008),
            selected=True,
            baseline_selected=True,
            severe_loss=False,
            baseline_severe_loss=False,
            mae_atr20=-0.3,
            baseline_mae_atr20=-0.3,
            point_in_time_parity=True,
        )
        for day in range(200)
        for stock in range(20)
    )
    return evaluate_terminal_holdout(
        TerminalHoldoutEvaluation(
            strategy="today",
            research_identity="score_today_historical_candidate_v1",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=rows,
        )
    )


def test_terminal_holdout_artifact_store_is_idempotent_and_detects_tampering(tmp_path) -> None:
    report = _report()
    store = TerminalHoldoutArtifactStore(tmp_path, strategy="today")

    assert store.write(report).content_hash == report.content_hash
    assert store.write(replace(report)).content_hash == report.content_hash
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    payload["candidate_hash"] = "c" * 64
    (tmp_path / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TerminalHoldoutArtifactConflictError):
        store.verify()
