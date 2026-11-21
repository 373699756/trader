from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

from tests.helpers.shared.configuration import RuntimeSettingsFixture
from trader.entrypoints.cli import BuildParser
from trader.infra.settings.data_paths import DataPaths
from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.baseline_identity_audit import BaselineIdentityAuditService
from trader.training.infra.research.baseline_identity_sources import LoadBaselineIdentityEvidence
from trader.training.infra.tomorrow_probability import (
    LogisticTomorrowModel,
    PublishTomorrowProbabilityModel,
    TomorrowProbabilityArtifact,
)
from trader.training.infra.tomorrow_settings import LoadTomorrowFitSettings

ROOT = Path(__file__).resolve().parents[4]


def test_baseline_audit_is_an_explicit_read_only_cli_command() -> None:
    parser = BuildParser()
    args = parser.parse_args(["research-baseline-audit"])
    assert args.command == "research-baseline-audit"


def test_baseline_audit_projection_has_no_future_or_production_authority_fields() -> None:
    source = (ROOT / "src/trader/training/entrypoints/commands.py").read_text(encoding="utf-8")
    assert "LoadBaselineIdentityEvidence" in source
    assert '"production_authority": audit.production_authority' in source
    assert "research-baseline-audit" in source


def test_baseline_audit_requires_distinct_production_artifacts(tmp_path: Path) -> None:
    runtime = replace(RuntimeSettingsFixture(), data_paths=DataPaths(tmp_path / "data"))

    initial = BaselineIdentityAuditService(LoadBaselineIdentityEvidence(runtime)).execute()
    assert all(f"tomorrow_probability_{profile}_target_500" in initial.unavailable for profile in ("v1", "v2", "v3", "v5"))
    assert not runtime.data_paths.root.exists()

    for profile in ("v2", "v3"):
        retired = runtime.data_paths.models / profile / "tomorrow" / "model.json"
        retired.parent.mkdir(parents=True)
        retired.write_text("retired model", encoding="utf-8")
    width = len(TOMORROW_FEATURE_NAMES)
    model = TomorrowProbabilityArtifact(
        "v3", PredictionTarget(Strategy.TOMORROW, 500), "a" * 64, LoadTomorrowFitSettings(runtime.project_root, "v3").identity,
        (date(2024, 1, 2),), (date(2024, 2, 2),), 0, 1,
        LogisticTomorrowModel(0, (0.0,) * width, (0.0,) * width, (1.0,) * width), None,
    )
    identity = PublishTomorrowProbabilityModel(runtime.data_paths.models, model)

    result = BaselineIdentityAuditService(LoadBaselineIdentityEvidence(runtime)).execute()
    claim = next(item for item in result.claims if item.name == "tomorrow_probability_v3_target_500")
    assert claim.actual == model.target.identity and claim.source_hash == identity
    assert claim.status == "matched"
    assert result.status == "baseline_identity_inconsistent"
    assert "tomorrow_probability_v2_target_500" in result.unavailable
    assert not result.production_authority
