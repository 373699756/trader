"""Read-only sources for the current baseline identity audit."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from trader.application.research.baseline_identity_audit import BaselineIdentityEvidence
from trader.domain.research.baseline_identity import BaselineIdentityClaim, source_hash
from trader.infra.research.tomorrow_historical_artifacts import (
    TomorrowHistoricalArtifactArchive,
    TomorrowHistoricalArtifactConflictError,
)
from trader.infra.scoring.profile_factory import load_scoring_profile
from trader.infra.settings import RuntimeSettings, load_strategy_settings


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class PackagedBaselineIdentityEvidence(BaselineIdentityEvidence):
    claims: tuple[BaselineIdentityClaim, ...]
    live_identity_available: bool = False

    def baseline_identity_claims(self) -> tuple[BaselineIdentityClaim, ...]:
        return self.claims


def load_baseline_identity_evidence(runtime: RuntimeSettings) -> PackagedBaselineIdentityEvidence:
    strategy = load_strategy_settings(runtime.strategy_config_path)
    strategy_hash = _file_hash(runtime.strategy_config_path)
    design_path = runtime.project_root / "docs/02_工程设计.md"
    strategy_doc_path = runtime.project_root / "docs/01_评分逻辑.md"
    v1 = load_scoring_profile("v1").identity
    v2 = load_scoring_profile("v2").identity
    historical_archive = TomorrowHistoricalArtifactArchive(runtime.runtime_dir / "tomorrow-historical")
    historical_source = runtime.runtime_dir / "tomorrow-historical"
    historical_conflict = False
    try:
        historical_report = historical_archive.read_report_payload()
    except TomorrowHistoricalArtifactConflictError:
        historical_report = None
        historical_conflict = True
    historical_report_hash = _optional_text(historical_report, "content_hash")
    historical_model_hash = _optional_text(historical_report, "model_artifact_hash")
    historical_status = "artifact_conflict" if historical_conflict else _optional_text(historical_report, "status")
    historical_hash_source = source_hash(str(historical_source / "historical-report.json"))
    historical_binding = (
        "conflict" if historical_conflict else "bound" if historical_report_hash and historical_model_hash else None
    )
    claims = (
        BaselineIdentityClaim(
            "active_profile",
            strategy.tomorrow_scoring_profile,
            strategy.tomorrow_scoring_profile,
            str(runtime.strategy_config_path),
            strategy_hash,
        ),
        BaselineIdentityClaim(
            "v1_model_identity",
            "residual_momentum_linear",
            v1.model_id,
            "trader.infra.scoring.profiles.v1/model.json",
            source_hash(v1.model_hash),
        ),
        BaselineIdentityClaim(
            "v2_model_identity",
            "daily_reconstructible_ensemble",
            v2.model_id,
            "trader.infra.scoring.profiles.v2/model.json",
            source_hash(v2.model_hash),
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
            source_hash("tomorrow-production-activation-policy"),
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
            _file_hash(strategy_doc_path) if strategy_doc_path.is_file() else source_hash(str(strategy_doc_path)),
        ),
        BaselineIdentityClaim(
            "business_design_document_present",
            "present",
            "present" if design_path.is_file() else None,
            str(design_path),
            _file_hash(design_path) if design_path.is_file() else source_hash(str(design_path)),
        ),
        BaselineIdentityClaim(
            "live_runtime_identity",
            "available",
            None,
            "running-runtime",
            source_hash("running-runtime"),
            required=False,
        ),
    )
    return PackagedBaselineIdentityEvidence(claims)


def _optional_text(payload: dict[str, object] | None, name: str) -> str | None:
    if payload is None:
        return None
    value = payload.get(name)
    return value if isinstance(value, str) else None


__all__ = ["PackagedBaselineIdentityEvidence", "load_baseline_identity_evidence"]
