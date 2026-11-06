from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.helpers.shared.python_source import source_text

from tests.helpers.shared.scoring_profiles import IsScoringProfilePath

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCANNED_ROOTS = (
    PROJECT_ROOT / "src" / "trader",
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "config",
    PROJECT_ROOT / ".agents" / "skills",
)
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".sh"}

# V1/V2/V3/V5 are reserved for user-selected scoring profiles and their
# model/training identities. External supplier names and URLs are not
# project version controls. Everything else must use a stable semantic name.
PROHIBITED_PATH = re.compile(
    r"(?:^|/)(?:v\d+)(?:/|$)|"
    r"(?:^|/)(?:test_)?v\d+[_-]|"
    r"(?:^|/)[^/]*_(?:v\d+)_(?:runtime|freezing|projection|adapters|helpers|issues)(?:\.|/|$)",
    re.IGNORECASE,
)
PROHIBITED_PROJECT_TOKEN = re.compile(
    r"\bV\d+(?:Runtime|Cycle|Freeze|Data|DeepSeek|Input|Market|Outcome|Overlay|Pipeline|"
    r"Refresh|Research|Review|Scheduler|Settlement|Supply|Trading|Control|Committed)|"
    r"\b(?:v\d+[_-])?(?:freeze_view|status|event|error|long_projection|freeze_identity|"
    r"committed_freeze|freeze_overlay|research_readiness|production_performance)[_-]v\d+\b|"
    r"\b(?:runtime|market|feature|cache|columnar|issuer|diagnostic|sampling|probe|report|audit|"
    r"progress|artifact|manifest|dataset|contract|policy|rule)[A-Za-z0-9_-]*[_-]v\d+\b",
    re.IGNORECASE,
)


def _tracked_paths() -> tuple[Path, ...]:
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return tuple(path for relative in tracked.split("\0") if relative and (path := PROJECT_ROOT / relative).is_file())


def test_non_scoring_code_and_contracts_use_stable_semantic_names() -> None:
    path_violations: list[str] = []
    content_violations: list[str] = []
    for path in _tracked_paths():
        if path.is_relative_to(PROJECT_ROOT / ".agents/skills/trader-delivery"):
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if any(path.is_relative_to(root) for root in SCANNED_ROOTS):
            if PROHIBITED_PATH.search(relative) and not IsScoringProfilePath(path.relative_to(PROJECT_ROOT)):
                path_violations.append(relative)
        if path.suffix not in TEXT_SUFFIXES or not any(path.is_relative_to(root) for root in SCANNED_ROOTS):
            continue
        try:
            content = source_text(path)
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if PROHIBITED_PROJECT_TOKEN.search(line):
                content_violations.append(f"{relative}:{line_number}")

    assert path_violations == []
    assert content_violations == []


def test_public_release_contract_uses_stable_schema_identities() -> None:
    release_contract = source_text(PROJECT_ROOT / "src/trader/web/static/release_contract.js")
    freeze_queries = source_text(PROJECT_ROOT / "src/trader/recommendation/application/pipeline/freeze_publish/read_only_queries.py")

    assert 'const STATUS_SCHEMA = "runtime_status";' in release_contract
    assert 'const FREEZE_VIEW_SCHEMA = "freeze_view";' in release_contract
    assert 'FREEZE_VIEW_SCHEMA_VERSION = "freeze_view"' in freeze_queries
    versioned_freeze_schema = re.compile(r"\bv\d+_freeze_view_v\d+\b", re.IGNORECASE)
    assert versioned_freeze_schema.search(release_contract) is None
    assert versioned_freeze_schema.search(freeze_queries) is None


def test_runtime_and_configuration_paths_are_not_project_versioned() -> None:
    assert (PROJECT_ROOT / "config/runtime.json").is_file()
    assert (PROJECT_ROOT / "config/strategy.json").is_file()
    retired_config_root = PROJECT_ROOT / "config" / ("v" + "2")
    assert not any(path.is_file() for path in retired_config_root.rglob("*"))

    runtime_config = source_text(PROJECT_ROOT / "config/runtime.json")
    assert '"runtime_dir": ".runtime/trader"' in runtime_config
    versioned_runtime_root = re.compile(r"\.runtime/v\d+", re.IGNORECASE)
    assert versioned_runtime_root.search(runtime_config) is None


def test_baostock_modules_contain_no_legacy_versioned_schema() -> None:
    source_paths = (
        PROJECT_ROOT / "src/trader/download/domain/baostock_daily.py",
        PROJECT_ROOT / "src/trader/download/infra/baostock_gateway.py",
        PROJECT_ROOT / "src/trader/download/infra/baostock_session.py",
        PROJECT_ROOT / "src/trader/download/infra/baostock_sync_supplier.py",
    )
    token_pattern = re.compile(r'"((?:score_)?baostock_[a-z0-9_]+_v\d+)"')
    tokens = {token for path in source_paths for token in token_pattern.findall(source_text(path))}
    assert tokens == set()


pytestmark = [pytest.mark.slow_static]
