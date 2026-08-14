"""Pure research-domain interface values."""

from trader.domain.research.baseline import mean_rank_ic, population_spearman, quantile_bucket, stock_net_contribution
from trader.domain.research.challengers import (
    R4_PARAMETER_SET_VERSION,
    ChallengerSpecification,
    ContinuousEntryAssessment,
    ContinuousEntryInputs,
    HeatWeakStructureAssessment,
    HeatWeakStructureInputs,
    assess_continuous_entry,
    assess_heat_weak_structure,
    challenger_parameter_manifest,
    challenger_registry,
)
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
from trader.domain.research.statistics import (
    BOOTSTRAP_MASTER_SEED,
    BOOTSTRAP_REPETITIONS,
    bootstrap_seed,
    holm_step_down,
    paired_moving_block_bootstrap,
)

__all__ = [
    "CostSettlementBasis",
    "BOOTSTRAP_MASTER_SEED",
    "BOOTSTRAP_REPETITIONS",
    "R4_PARAMETER_SET_VERSION",
    "ChallengerSpecification",
    "ContinuousEntryAssessment",
    "ContinuousEntryInputs",
    "HeatWeakStructureAssessment",
    "HeatWeakStructureInputs",
    "HistoricalCandidateSummary",
    "ResearchDataLineage",
    "ResearchSelectionPool",
    "ScoreComponent",
    "coverage_shrunk_score",
    "assess_continuous_entry",
    "assess_heat_weak_structure",
    "challenger_parameter_manifest",
    "challenger_registry",
    "bootstrap_seed",
    "holm_step_down",
    "mean_rank_ic",
    "optimistic_component_upper_bound",
    "optimistic_final_upper_bound",
    "population_spearman",
    "paired_moving_block_bootstrap",
    "quantile_bucket",
    "stock_net_contribution",
]
