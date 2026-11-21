"""Content identity for training and its pure feature/label dependencies."""

from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from collections.abc import Iterable

from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId

_SHARED_INPUT = (
    "download/domain/history_control.py", "download/domain/history_revision.py",
    "download/infra/history_control_index.py", "download/infra/history_prices.py", "download/infra/history_snapshot_reader.py",
    "recommendation/domain/market/models.py", "recommendation/domain/outcomes/prediction_target.py",
    "recommendation/domain/outcomes/tomorrow_target.py", "training/infra/logistic_fitting.py",
)
_TOMORROW = (
    "training/infra/tomorrow/artifact.py", "training/infra/tomorrow/settings.py", "training/infra/tomorrow/training.py",
    "recommendation/domain/market/tomorrow_probability.py",
)
_D25 = (
    "download/domain/current_industry.py", "training/domain/training_input_identity.py",
    "recommendation/domain/market/history_alignment.py", "training/infra/history/aligned_training_rows.py",
    "training/infra/history/history_training_input.py", "training/infra/d25/artifact.py",
    "training/infra/d25/artifact_values.py",
    "training/infra/d25/calibration.py", "training/infra/d25/labels.py",
)
_D25_STANDARD = (
    "recommendation/domain/market/feature_contracts.py", "recommendation/domain/scoring/residualization.py",
    "training/domain/final_fit.py", "training/domain/label_benchmark.py", "training/domain/production_diagnostic_dates.py",
    "training/infra/d25/training.py", "training/infra/engine.py", "training/infra/model_fitting.py",
    "training/infra/model_evaluation.py", "training/infra/model_fitting_results.py",
    "training/infra/sample_builder.py", "training/infra/sample_cache.py", "training/infra/sample_index.py",
    "training/infra/sample_schema.py", "training/infra/sample_sequences.py", "training/infra/training_contract.py",
    "training/infra/training_settings.py",
)
_RANKING = ("training/domain/v1/learned_rank.py", "training/infra/learned_rank_settings.py", "training/infra/ranking.py")
_TCN = (
    "training/infra/price_volume.py", "recommendation/domain/market/v5/features.py",
    "training/infra/tcn/binary_fitting.py", "training/infra/tcn/configuration.py", "training/infra/tcn/manifest.py",
    "training/infra/tcn/network.py", "training/infra/tcn/settings.py", "training/infra/tcn/tensors.py",
    "training/infra/tcn/transforms.py",
)


def _digest(root: Path, files: Iterable[Path]) -> str:
    digest = sha256()
    for path in sorted(set(files)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


@lru_cache(maxsize=8)
def LaneTrainingImplementationIdentity(strategy: Strategy, profile: ScoringProfileId) -> str:
    """Hash the strategy/profile owner and the sources that affect its input or fitted artifact."""
    if strategy not in {Strategy.TOMORROW, Strategy.D25}:
        raise ValueError("strategy_has_no_training_implementation")
    root = Path(__file__).resolve().parents[2]
    dependencies = list(_SHARED_INPUT)
    if strategy is Strategy.TOMORROW:
        dependencies.extend(_TOMORROW)
        if profile in {"v1", "v2"}:
            dependencies.append("training/infra/tomorrow/tree.py")
    else:
        dependencies.extend(_D25)
        dependencies.extend(
            str(path.relative_to(root)) for path in (root / "training/infra/model_bundles").glob("*.py")
        )
        if profile == "v1":
            dependencies.extend((
                "recommendation/domain/market/v1/features.py", "recommendation/domain/scoring/utility_scoring.py",
                "training/domain/final_fit.py", "training/domain/label_benchmark.py",
                "training/domain/v1/training_plan.py", "training/infra/sample_schema.py",
            ))
        else:
            dependencies.extend(_D25_STANDARD)
    if profile == "v1":
        dependencies.extend(_RANKING)
    else:
        dependencies.append("training/infra/fitting_settings.py")
    if strategy is Strategy.D25 and profile in {"v1", "v2"}:
        dependencies.append("training/infra/d25/tree.py")
    if profile == "v5":
        dependencies.extend(_TCN)
    lane = root / "training/infra" / strategy.value / profile
    return _digest(root, [*(root / name for name in dependencies), *lane.glob("*.py")])


@lru_cache(maxsize=1)
def TrainingImplementationIdentity() -> str:
    """Whole-tree provenance for explicitly invoked offline research."""
    root = Path(__file__).resolve().parents[2]
    files = [
        path for directory in ("training", "download/domain", "recommendation/domain")
        for path in (root / directory).rglob("*.py")
    ]
    return _digest(root, (*files, root / "download/infra/history_prices.py"))


__all__ = ["LaneTrainingImplementationIdentity", "TrainingImplementationIdentity"]
