from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "trader"

_NUMBERED_STAGE = re.compile(r"(?:^|[_-])(?:p|r)\d+(?=[_.\-/]|$)", re.IGNORECASE)
_NUMBERED_VERSION = re.compile(r"(?:^|[_-])v\d+(?=[_.\-/]|$)", re.IGNORECASE)
_SCORING_PROFILE_ROOT = SOURCE / "infra" / "scoring" / "profiles"
_SCORING_PROFILE_TEST_ROOT = ROOT / "tests" / "unit" / "infra" / "scoring"
_PERCENTILE_METRIC = re.compile(r"(?:^|_)p\d+_(?:ms|seconds)(?:$|_)", re.IGNORECASE)


def _is_scoring_profile_path(path: Path) -> bool:
    try:
        relative = path.relative_to(_SCORING_PROFILE_ROOT)
    except ValueError:
        scoring_unit_test = path.parent == _SCORING_PROFILE_TEST_ROOT and re.fullmatch(
            r"test_v[123]_[a-z0-9_]+\.py", path.name
        )
        scoring_document_test = path == ROOT / "tests" / "contract" / "test_tomorrow_v3_document_contract.py"
        return scoring_unit_test or scoring_document_test
    return bool(relative.parts) and relative.parts[0] in {"v1", "v2", "v3"}


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
            elif _NUMBERED_VERSION.search(rendered) and not _is_scoring_profile_path(path):
                violations.append(rendered)

    assert violations == []


def test_active_configuration_has_no_numbered_version_controllers() -> None:
    violations: list[str] = []

    def inspect(value: object, location: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{location}.{key}"
                if key == "version" or key.endswith("_version") or key.endswith("_versions"):
                    violations.append(child)
                if (_NUMBERED_STAGE.search(key) and not _PERCENTILE_METRIC.search(key)) or _NUMBERED_VERSION.search(
                    key
                ):
                    violations.append(child)
                inspect(item, child)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                inspect(item, f"{location}[{index}]")
        elif isinstance(value, str):
            if value in {"v1", "v2", "v3"}:
                return
            if value.startswith("deepseek-v"):
                return
            if _NUMBERED_STAGE.search(value) or _NUMBERED_VERSION.search(value):
                violations.append(location)

    for path in sorted((ROOT / "config").rglob("*.json")):
        inspect(json.loads(path.read_text(encoding="utf-8")), path.name)

    assert sorted(set(violations)) == []


def test_production_feature_contract_has_no_historical_stage_prefixes() -> None:
    production_files = (
        SOURCE / "domain" / "market" / "factors.py",
        SOURCE / "application" / "recommendation" / "tomorrow_model_scoring.py",
        SOURCE / "infra" / "market_data" / "normalization" / "features.py",
        SOURCE / "entrypoints" / "performance.py",
    )
    violations = [
        str(path.relative_to(ROOT))
        for path in production_files
        if re.search(r"[\"']p[1-9]_", path.read_text(encoding="utf-8"), re.IGNORECASE)
    ]

    assert violations == []
