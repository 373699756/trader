from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.helpers.scoring_profiles import IsScoringProfilePath
from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "trader"

_NUMBERED_STAGE = re.compile(r"(?:^|[_-])(?:p|r)\d+(?=[_.\-/]|$)", re.IGNORECASE)
_NUMBERED_VERSION = re.compile(r"(?:^|[_-])v\d+(?=[_.\-/]|$)", re.IGNORECASE)
_PERCENTILE_METRIC = re.compile(r"(?:^|_)p\d+_(?:ms|seconds)(?:$|_)", re.IGNORECASE)


@pytest.mark.parametrize(
    ("relative", "allowed"),
    (
        ("src/trader/training/infra/profile/v1/training.py", True),
        ("src/trader/recommendation/domain/market/v5/features.py", True),
        ("tests/contract/test_v5_web_contract.py", True),
        ("scripts/training/models/v5_training.py", True),
        ("scripts/training/samples/v5_samples.py", True),
        ("src/trader/recommendation/application/runtime/v5/worker.py", False),
        ("src/trader/training/infra/profile/v4/training.py", False),
        ("scripts/runtime_diagnostics/runtime_" + "v5.py", False),
    ),
)
def test_profile_names_are_allowed_only_under_scoring_owners(relative, allowed) -> None:
    assert IsScoringProfilePath(Path(relative)) is allowed


def test_numbered_names_are_owned_only_by_scoring_profile_directories() -> None:
    roots = (SOURCE, ROOT / "scripts", ROOT / "tests")
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT)
            rendered = relative.as_posix()
            if _NUMBERED_STAGE.search(rendered):
                violations.append(rendered)
            elif _NUMBERED_VERSION.search(rendered) and not IsScoringProfilePath(relative):
                violations.append(rendered)

    assert violations == []


def _configuration_version_controllers(document: object, document_name: str) -> list[str]:
    violations: list[str] = []

    def inspect(value: object, location: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{location}.{key}"
                if key == "version" or key.endswith("_version") or key.endswith("_versions"):
                    violations.append(child)
                profile_sample_resource = (
                    location in {"training.json.resources.hdd.samples", "training.json.resources.ssd.samples"}
                    and key in {"v1_batch_rows", "v1_sqlite_cache_bytes"}
                )
                if not profile_sample_resource and (
                    (_NUMBERED_STAGE.search(key) and not _PERCENTILE_METRIC.search(key))
                    or _NUMBERED_VERSION.search(key)
                ):
                    violations.append(child)
                inspect(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                inspect(item, f"{location}[{index}]")
        elif isinstance(value, str):
            if value in SCORING_PROFILE_IDS:
                return
            if value.startswith("deepseek-v"):
                return
            if _NUMBERED_STAGE.search(value) or _NUMBERED_VERSION.search(value):
                violations.append(location)

    inspect(document, document_name)
    return violations


@pytest.mark.parametrize("medium", ("hdd", "ssd"))
def test_profile_sample_resources_are_owned_only_by_their_declared_configuration_path(medium: str) -> None:
    location = f"training.json.resources.{medium}.samples"
    assert _configuration_version_controllers({"v1_batch_rows": 256, "v1_sqlite_cache_bytes": 1024}, location) == []


@pytest.mark.parametrize(
    ("location", "key"),
    (
        ("runtime.json.resources.hdd.samples", "v1_batch_rows"),
        ("nested/training.json.resources.hdd.samples", "v1_batch_rows"),
        ("training.json.resources.hdd.cpu", "v1_batch_rows"),
        ("training.json.resources.hdd.samples.nested", "v1_sqlite_cache_bytes"),
        ("training.json.resources.ssd.samples", "v2_batch_rows"),
        ("training.json.resources.ssd.samples", "v1_other_budget"),
    ),
)
def test_other_numbered_configuration_keys_remain_rejected(location: str, key: str) -> None:
    assert _configuration_version_controllers({key: 256}, location) == [f"{location}.{key}"]


def test_runtime_configuration_has_no_numbered_version_controllers() -> None:
    violations = [
        violation
        for path in sorted((ROOT / "config").rglob("*.json"))
        for violation in _configuration_version_controllers(
            json.loads(path.read_text(encoding="utf-8")), path.relative_to(ROOT / "config").as_posix()
        )
    ]

    assert sorted(set(violations)) == []


def test_production_feature_contract_has_no_historical_stage_prefixes() -> None:
    production_files = (
        SOURCE / "recommendation" / "domain" / "market" / "factors.py",
        SOURCE / "recommendation" / "application" / "pipeline" / "local_score" / "model_scoring.py",
        SOURCE / "recommendation" / "infra" / "normalization" / "features.py",
        SOURCE / "entrypoints" / "performance.py",
    )
    violations = [
        str(path.relative_to(ROOT))
        for path in production_files
        if re.search(r"[\"']p[1-9]_", path.read_text(encoding="utf-8"), re.IGNORECASE)
    ]

    assert violations == []
