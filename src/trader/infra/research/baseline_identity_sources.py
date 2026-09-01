"""Read-only sources for the current baseline identity audit."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from trader.application.research.baseline_identity_audit import BaselineIdentityEvidence
from trader.domain.research.baseline_identity import BaselineIdentityClaim, source_hash
from trader.infra.research.tomorrow_historical_p2_artifacts import (
    TomorrowHistoricalP2ArtifactConflictError,
    TomorrowHistoricalP2ArtifactStore,
)
from trader.infra.settings import RuntimeSettings, load_strategy_settings
from trader.infra.tomorrow_production_model import load_packaged_tomorrow_production_model


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
    design_path = runtime.project_root / "docs/software-business-design.md"
    strategy_doc_path = runtime.project_root / "docs/recommendation-strategy.md"
    v1 = load_packaged_tomorrow_production_model("v1")
    v2 = load_packaged_tomorrow_production_model("v2")
    p2_store = TomorrowHistoricalP2ArtifactStore(runtime.runtime_dir / "score-tomorrow-p2")
    p2_source = runtime.runtime_dir / "score-tomorrow-p2"
    p2_conflict = False
    try:
        p2_report = p2_store.read_report_payload()
    except TomorrowHistoricalP2ArtifactConflictError:
        p2_report = None
        p2_conflict = True
    p2_report_hash = _optional_text(p2_report, "content_hash")
    p2_model_hash = _optional_text(p2_report, "model_artifact_hash")
    p2_status = "artifact_conflict" if p2_conflict else _optional_text(p2_report, "status")
    p2_hash_source = source_hash(str(p2_source / "historical-report.json"))
    p2_binding = "conflict" if p2_conflict else "bound" if p2_report_hash and p2_model_hash else None
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
            "v1_manual_residual_momentum_v1",
            v1.model_id,
            "trader.resources.models.tomorrow_v1_model.json",
            source_hash(v1.model_hash),
        ),
        BaselineIdentityClaim(
            "v2_model_identity",
            "daily_reconstructible_ensemble_v1",
            v2.model_id,
            "trader.resources.models.tomorrow_p2_model.json",
            source_hash(v2.model_hash),
        ),
        BaselineIdentityClaim(
            "p2_historical_conclusion",
            "historical_rejected",
            p2_status,
            str(p2_source / "historical-report.json"),
            p2_hash_source,
        ),
        BaselineIdentityClaim(
            "p2_model_report_binding",
            "bound",
            p2_binding,
            str(p2_source / "historical-report.json"),
            p2_hash_source,
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
            "running-v2-runtime",
            source_hash("running-v2-runtime"),
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
