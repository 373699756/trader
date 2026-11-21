"""Read-only sources for the current baseline identity audit."""

from __future__ import annotations

from dataclasses import dataclass

from trader.infra.serialization.canonical import FileSha256
from trader.infra.settings import RuntimeSettings
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS
from trader.recommendation.infra.settings.loading import LoadStrategySettings
from trader.training.application.baseline_identity_audit import BaselineIdentityEvidence
from trader.training.domain.evaluation.baseline_identity import BaselineIdentityClaim, SourceHash
from trader.training.domain.evaluation.tomorrow_historical import TOMORROW_HISTORICAL_SPEC
from trader.training.infra.tomorrow_probability import LoadTomorrowProbabilityModel, TomorrowProbabilityModelPath
from trader.training.infra.research.tomorrow_historical_evidence import (
    TomorrowHistoricalEvidenceConflictError,
    TomorrowHistoricalEvidenceIndex,
)


@dataclass(frozen=True)
class PackagedBaselineIdentityEvidence(BaselineIdentityEvidence):
    claims: tuple[BaselineIdentityClaim, ...]
    live_identity_available: bool = False

    def baseline_identity_claims(self) -> tuple[BaselineIdentityClaim, ...]:
        return self.claims


def LoadBaselineIdentityEvidence(runtime: RuntimeSettings) -> PackagedBaselineIdentityEvidence:
    strategy = LoadStrategySettings(runtime.strategy_config_path)
    strategy_hash = FileSha256(runtime.strategy_config_path)
    design_path = runtime.project_root / "docs/b_系统设计.md"
    strategy_doc_path = runtime.project_root / "docs/c_评分设计.md"
    model_claims = tuple(
        _tomorrow_model_claim(runtime, target)
        for target, _members in strategy.prediction_targets.tomorrow_groups(SCORING_PROFILE_IDS)
    )
    historical_root = runtime.data_paths.research(strategy.scoring_profile) / "experiments"
    historical_evidence = TomorrowHistoricalEvidenceIndex(historical_root)
    historical_source = str(historical_root / TOMORROW_HISTORICAL_SPEC.research_identity / "historical-report.json")
    historical_conflict = False
    try:
        historical_report = historical_evidence.read_report_payload()
    except TomorrowHistoricalEvidenceConflictError:
        historical_report = None
        historical_conflict = True
    historical_report_hash = _optional_text(historical_report, "content_hash")
    historical_model_hash = _optional_text(historical_report, "model_artifact_hash")
    historical_status = "artifact_conflict" if historical_conflict else _optional_text(historical_report, "status")
    historical_hash_source = historical_report_hash or SourceHash(historical_source)
    historical_binding = (
        "conflict" if historical_conflict else "bound" if historical_report_hash and historical_model_hash else None
    )
    claims = (
        BaselineIdentityClaim(
            "configured_profile",
            strategy.scoring_profile,
            strategy.scoring_profile,
            str(runtime.strategy_config_path),
            strategy_hash,
        ),
        *model_claims,
        BaselineIdentityClaim(
            "historical_screening_conclusion",
            "historical_rejected",
            historical_status,
            historical_source,
            historical_hash_source,
            required=False,
        ),
        BaselineIdentityClaim(
            "historical_model_report_binding",
            "bound",
            historical_binding,
            historical_source,
            historical_hash_source,
            required=False,
        ),
        BaselineIdentityClaim(
            "strategy_source_hash",
            strategy_hash,
            strategy_hash,
            str(runtime.strategy_config_path),
            strategy_hash,
        ),
        BaselineIdentityClaim(
            "strategy_document_present",
            "present",
            "present" if strategy_doc_path.is_file() else None,
            str(strategy_doc_path),
            FileSha256(strategy_doc_path) if strategy_doc_path.is_file() else SourceHash(str(strategy_doc_path)),
        ),
        BaselineIdentityClaim(
            "business_design_document_present",
            "present",
            "present" if design_path.is_file() else None,
            str(design_path),
            FileSha256(design_path) if design_path.is_file() else SourceHash(str(design_path)),
        ),
        BaselineIdentityClaim(
            "live_runtime_identity",
            "available",
            None,
            "running-runtime",
            SourceHash("running-runtime"),
            required=False,
        ),
    )
    return PackagedBaselineIdentityEvidence(claims)


def _tomorrow_model_claim(runtime: RuntimeSettings, target: PredictionTarget) -> BaselineIdentityClaim:
    models = runtime.data_paths.models
    source = str(TomorrowProbabilityModelPath(models, target))
    identity = SourceHash(source)
    actual: str | None = None
    try:
        model = LoadTomorrowProbabilityModel(models, target)
    except (OSError, ValueError):
        actual = "artifact_conflict"
    else:
        if model is not None:
            actual, identity = model.artifact.target.identity, model.identity
    return BaselineIdentityClaim(f"tomorrow_probability_target_{target.threshold_bps}", target.identity, actual, source, identity)


def _optional_text(payload: dict[str, object] | None, name: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(name)
    return value if isinstance(value, str) else None


__all__ = ["PackagedBaselineIdentityEvidence", "LoadBaselineIdentityEvidence"]
