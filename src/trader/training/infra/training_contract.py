"""One training-contract identity shared by fitting and maintenance observations."""

from __future__ import annotations

from importlib.metadata import version

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.history_alignment import HISTORY_ALIGNMENT_CONTRACT
from trader.training.application.tomorrow_training import TOMORROW_TRAINING_COMPUTE_THREADS, TOMORROW_TRAINING_PEAK_RSS_MIB
from trader.training.domain.final_fit import FINAL_FIT_CONTRACT
from trader.training.domain.label_benchmark import TRAINING_LABEL_BENCHMARK_CONTRACT
from trader.training.domain.production_diagnostic_dates import PRODUCTION_DIAGNOSTIC_CONTRACT
from trader.training.domain.training_input_identity import TRAINING_INDUSTRY_POLICY
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.model_evaluation import RECENT_PREDICTION_EVALUATION_CONTRACT
from trader.training.infra.model_fitting import FittingParameterDocument, LightGBMFittingParameters
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_settings import ProfileTrainingSettings


def BuildTrainingContractHash(
    profile: TrainedProfileContract,
    contract: TrainedHeadContract,
    settings: ProfileTrainingSettings,
    *,
    compute_threads: int = TOMORROW_TRAINING_COMPUTE_THREADS,
) -> str:
    fitting = settings.fitting
    return ContentHash(
        {
            "implementation_identity": TrainingImplementationIdentity(),
            "dependencies": {"numpy": version("numpy"), "lightgbm": version("lightgbm")}
            if profile.algorithm == "lightgbm"
            else {"numpy": version("numpy"), "torch": version("torch")}
            if profile.algorithm == "tcn"
            else {"numpy": version("numpy")},
            "schema_version": f"{profile.profile_id}_head_training_contract",
            "profile_id": profile.profile_id,
            "history_sessions": profile.history_sessions,
            "feature_day_axis": HISTORY_ALIGNMENT_CONTRACT,
            "fit_dates": settings.fit_dates,
            "algorithm": profile.algorithm,
            "strategy_head": contract.strategy.value,
            "compute_threads": fitting.compute_threads
            if isinstance(fitting, TcnFittingParameters)
            else compute_threads,
            "peak_rss_mib": TOMORROW_TRAINING_PEAK_RSS_MIB,
            "training_input_scope": "complete_manifest",
            "feature_manifest_hash": contract.feature_manifest.content_hash,
            "feature_ids": list(contract.feature_manifest.names),
            "feature_units": list(contract.feature_manifest.units),
            "label_target": contract.label_target,
            "maturity_sessions": contract.maturity_sessions,
            "benchmark": TRAINING_LABEL_BENCHMARK_CONTRACT,
            "training_cost_bps": 0,
            "evaluation_cost_bps": [20, 50, 100],
            "minimum_industry_training_rows": fitting.minimum_industry_training_rows,
            "prediction_evaluation_contract": RECENT_PREDICTION_EVALUATION_CONTRACT,
            "diagnostic_date_contract": PRODUCTION_DIAGNOSTIC_CONTRACT,
            "final_fit_contract": FINAL_FIT_CONTRACT,
            "industry_policy": TRAINING_INDUSTRY_POLICY,
            "model_parameters": FittingParameterDocument(fitting)
            if isinstance(fitting, TcnFittingParameters)
            else {"ridge_penalty": fitting.penalty}
            if not isinstance(fitting, LightGBMFittingParameters)
            else {
                "objective": "regression_l2",
                "learning_rate": fitting.learning_rate,
                "max_depth": fitting.max_depth,
                "num_leaves": fitting.num_leaves,
                "min_data_in_leaf": fitting.minimum_leaf_rows,
                "num_boost_round": fitting.boosting_rounds,
                "early_stopping_rounds": fitting.early_stopping_rounds,
                "max_bin": fitting.maximum_bins,
                "deterministic": True,
                "seed": fitting.seed,
                "force_col_wise": True,
                "histogram_pool_size_mib": fitting.histogram_pool_mib,
            },
            "parallel_industries": 1,
            "calibration": "frozen_recent_diagnostic_linear_transfer_to_final_refit",
            "training_anchor": "15:00_close_proxy",
            "runtime_anchor": contract.runtime_anchor,
            "point_in_time_parity": False,
            "validation_scope": "daily_close_engineering_proxy",
            "research_holdout_status": "not_part_of_recent_production_diagnostic",
            "automatic_model_update": False,
            "production_authority": False,
        }
    )


__all__ = ["BuildTrainingContractHash"]
