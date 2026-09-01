"""Typed rows and immutable reports for daily trend screening."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.replay_models import canonical_hash
from trader.application.research.score_r6_models import ScoreR6Board, ScoreR6Metrics
from trader.domain.research.score_r6_daily import ScoreR6DailyCandidate

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class ScoreR6DailyRow:
    trade_date: date
    code: str
    board: ScoreR6Board
    momentum_20_score: float
    residual_momentum_score: float
    trend_efficiency_score: float
    downside_stability_score: float
    drawdown_recovery_score: float
    liquidity_score: float
    residual_return_60_5_pct: float
    recent_return_5d_pct: float
    close_ma20_spread_pct: float
    drawdown_60d_pct: float
    downside_volatility_20d_pct: float
    volatility_20d_pct: float
    return_5d_pct: float

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("daily trend code is invalid")
        if self.board not in {"main", "chinext", "star"}:
            raise ValueError("daily trend board is invalid")
        scores = (
            self.momentum_20_score,
            self.residual_momentum_score,
            self.trend_efficiency_score,
            self.downside_stability_score,
            self.drawdown_recovery_score,
            self.liquidity_score,
        )
        if any(not math.isfinite(value) or not 0.0 <= value <= 100.0 for value in scores):
            raise ValueError("daily trend component scores must be in [0, 100]")
        raw = (
            self.residual_return_60_5_pct,
            self.recent_return_5d_pct,
            self.close_ma20_spread_pct,
            self.drawdown_60d_pct,
            self.downside_volatility_20d_pct,
            self.volatility_20d_pct,
            self.return_5d_pct,
        )
        if any(not math.isfinite(value) for value in raw):
            raise ValueError("daily trend raw values and label must be finite")
        if self.downside_volatility_20d_pct < 0.0 or self.volatility_20d_pct < 0.0:
            raise ValueError("daily trend volatility must be nonnegative")


@dataclass(frozen=True)
class ScoreR6DailyReport:
    status: Literal["insufficient_coverage", "historical_rejected", "historical_validated"]
    research_identity: str
    research_spec_hash: str
    archive: HistoricalArchiveStatus
    archive_manifest: HistoricalArchiveManifest
    selected_candidate: ScoreR6DailyCandidate | None
    training: ScoreR6Metrics
    validation: ScoreR6Metrics
    baseline_training: ScoreR6Metrics
    baseline_validation: ScoreR6Metrics
    historical_gate_passed: bool
    failure_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    promotion_authority: bool
    schema_version: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.research_spec_hash) is None:
            raise ValueError("daily trend report spec hash is invalid")
        if self.status == "historical_validated" and not self.historical_gate_passed:
            raise ValueError("daily trend validated status requires the historical gate")
        if self.status != "historical_validated" and self.historical_gate_passed:
            raise ValueError("daily trend historical pass requires validated status")
        if self.status == "insufficient_coverage" and self.selected_candidate is not None:
            raise ValueError("daily trend coverage rejection cannot freeze a candidate")
        if self.promotion_authority:
            raise ValueError("daily trend historical report cannot promote production")
        if self.schema_version != "score_r6_daily_trend_report_v1":
            raise ValueError("daily trend report schema is invalid")
        object.__setattr__(self, "failure_reasons", tuple(sorted(set(self.failure_reasons))))
        object.__setattr__(self, "limitations", tuple(sorted(set(self.limitations))))
        object.__setattr__(self, "content_hash", canonical_hash(self))


__all__ = ["ScoreR6DailyReport", "ScoreR6DailyRow"]
