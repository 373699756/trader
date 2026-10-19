"""Read-only sources for the current baseline identity audit."""

from __future__ import annotations

from dataclasses import dataclass

from trader.infra.serialization.canonical import FileSha256
from trader.infra.settings import LoadStrategySettings, RuntimeSettings
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.application.baseline_identity_audit import BaselineIdentityEvidence
from trader.training.domain.evaluation.baseline_identity import BaselineIdentityClaim, SourceHash
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
    design_path = runtime.project_root / "docs/2_工程设计.md"
    strategy_doc_path = runtime.project_root / "docs/1_评分逻辑.md"
    v2 = LoadScoringProfile("v2", training_root=runtime.data_paths.models).heads[Strategy.TOMORROW].predictor
    v3 = LoadScoringProfile("v3", training_root=runtime.data_paths.models).heads[Strategy.TOMORROW].predictor
    historical_archive = TomorrowHistoricalEvidenceIndex(
        runtime.data_paths.research(strategy.scoring_profile) / "experiments"
    )
    historical_source = runtime.data_paths.research(strategy.scoring_profile) / "experiments"
    historical_conflict = False
    try:
        historical_report = historical_archive.read_report_payload()
    except TomorrowHistoricalEvidenceConflictError:
        historical_report = None
        historical_conflict = True
    historical_report_hash = _optional_text(historical_report, "content_hash")
    historical_model_hash = _optional_text(historical_report, "model_artifact_hash")
    historical_status = "artifact_conflict" if historical_conflict else _optional_text(historical_report, "status")
    historical_hash_source = SourceHash(str(historical_source / "historical-report.json"))
    historical_binding = (
        "conflict" if historical_conflict else "bound" if historical_report_hash and historical_model_hash else None
    )
    claims = (
        BaselineIdentityClaim(
            "active_profile",
            strategy.scoring_profile,
            strategy.scoring_profile,
            str(runtime.strategy_config_path),
            strategy_hash,
        ),
        BaselineIdentityClaim(
            "v2_model_identity",
            "industry_ridge_lightgbm",
            v2.model_id,
            str(runtime.data_paths.models / "v2/tomorrow/model.json"),
            SourceHash(v2.model_hash),
        ),
        BaselineIdentityClaim(
            "v3_model_identity",
            "industry_ridge_lightgbm",
            v3.model_id,
            str(runtime.data_paths.models / "v3/tomorrow/model.json"),
            SourceHash(v3.model_hash),
        ),
        BaselineIdentityClaim(
            "historical_screening_conclusion",
            "historical_rejected",
            historical_status,
            str(historical_source / "historical-report.json"),
            historical_hash_source,
        ),
        BaselineIdentityClaim(
            "historical_model_report_binding",
            "bound",
            historical_binding,
            str(historical_source / "historical-report.json"),
            historical_hash_source,
            required=False,
        ),
        BaselineIdentityClaim(
            "production_authorization_basis",
            "manual_user_override",
            "manual_user_override",
            "tomorrow-production-activation-policy",
            SourceHash("tomorrow-production-activation-policy"),
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


def _optional_text(payload: dict[str, object] | None, name: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(name)
    return value if isinstance(value, str) else None


__all__ = ["PackagedBaselineIdentityEvidence", "LoadBaselineIdentityEvidence"]
