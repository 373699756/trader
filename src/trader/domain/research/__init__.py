"""Pure research-domain interface values."""

from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.historical import (
    CostSettlementBasis,
    HistoricalCandidateSummary,
    ResearchDataLineage,
    ResearchSelectionPool,
    ScoreComponent,
    coverage_shrunk_score,
    optimistic_component_upper_bound,
    optimistic_final_upper_bound,
)

__all__ = [
    "CostSettlementBasis",
    "HistoricalCandidateSummary",
    "ResearchDataLineage",
    "ResearchSelectionPool",
    "ScoreComponent",
    "coverage_shrunk_score",
    "mean_rank_ic",
    "optimistic_component_upper_bound",
    "optimistic_final_upper_bound",
    "population_spearman",
    "quantile_bucket",
    "stock_net_contribution",
]
