from __future__ import annotations

from typing import Dict, List

import pandas as pd

from . import candidate_filters, market_regime as market_regime_core, scoring_math


class FeatureBuilder:
    """Build normalized candidate frames and reusable scoring context."""

    def prepare_candidates(self, quotes: pd.DataFrame) -> pd.DataFrame:
        return candidate_filters.prepare_candidates(quotes)

    def candidate_filter_report(self, quotes: pd.DataFrame) -> Dict[str, object]:
        return candidate_filters.candidate_filter_report(quotes)

    def market_regime(self, df: pd.DataFrame, breadth_source: pd.DataFrame = None) -> Dict[str, object]:
        return market_regime_core.build_market_regime(df, breadth_source=breadth_source)

    def score_context(self, df: pd.DataFrame, industry_strength: Dict[str, float] = None) -> Dict[str, List[float]]:
        return scoring_math._score_context(df, industry_strength or {})

    def market_regime_with_history(self, market_regime: Dict[str, object], df: pd.DataFrame) -> Dict[str, object]:
        return market_regime_core._market_regime_with_history(market_regime, df)

    def row_speed(self, row: pd.Series) -> float:
        return scoring_math._row_speed(row)
