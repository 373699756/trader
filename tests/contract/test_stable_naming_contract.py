from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (
    PROJECT_ROOT / "src" / "trader",
    PROJECT_ROOT / "tests",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "config",
)
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".sh"}

# These are immutable BaoStock checkpoint identities written before the stable
# naming cleanup.  They are decode-only compatibility values, not project
# version controls; no other BaoStock ``*_vN`` value is permitted.
LEGACY_BAOSTOCK_PERSISTENCE_SCHEMAS = frozenset(
    {
        "baostock_exchange_calendar_v2",
        "baostock_daily_fact_v1",
        "baostock_industry_interval_v1",
        "baostock_code_download_v1",
        "baostock_daily_coverage_audit_v2",
        "baostock_partition_ref_v1",
        "baostock_daily_manifest_v3",
        "baostock_daily_shard_snapshot_v2",
    }
)

# V1/V2/V3 are reserved for the user-selected Tomorrow scoring profiles and
# their model/training identities. External supplier names and URLs are not
# project version controls. Everything else must use a stable semantic name.
ALLOWED_SCORING_PATH = re.compile(
    r"(?:tomorrow[_-]v[123](?:[_-](?:model|training|input|dataset))?|"
    r"tomorrow_v1_model\.json|test_tomorrow_v3_|package_tomorrow_v1_model)"
)
PROHIBITED_PATH = re.compile(
    r"(?:^|/)(?:v\d+)(?:/|$)|"
    r"(?:^|/)(?:test_)?v\d+[_-]|"
    r"(?:^|/)[^/]*_(?:v\d+)_(?:runtime|freezing|projection|adapters|helpers|issues)(?:\.|/|$)",
    re.IGNORECASE,
)
PROHIBITED_PROJECT_TOKEN = re.compile(
    r"\bV\d+(?:Runtime|Cycle|Decision|Data|DeepSeek|Freeze|Input|Market|Outcome|Overlay|Pipeline|"
    r"Refresh|Research|Review|Scheduler|Settlement|Supply|Trading|Control|Committed)|"
    r"\b(?:v\d+[_-])?(?:decision_view|status|event|error|long_projection|decision_identity|"
    r"committed_decision|decision_overlay|research_readiness|production_performance)[_-]v\d+\b|"
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
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if any(path.is_relative_to(root) for root in SCANNED_ROOTS):
            if PROHIBITED_PATH.search(relative) and not ALLOWED_SCORING_PATH.search(relative):
                path_violations.append(relative)
        if path.suffix not in TEXT_SUFFIXES or not any(path.is_relative_to(root) for root in SCANNED_ROOTS):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if PROHIBITED_PROJECT_TOKEN.search(line):
                content_violations.append(f"{relative}:{line_number}")

    assert path_violations == []
    assert content_violations == []


def test_public_release_contract_uses_stable_schema_identities() -> None:
    release_contract = (PROJECT_ROOT / "src/trader/web/static/release_contract.js").read_text(encoding="utf-8")
    decision_queries = (PROJECT_ROOT / "src/trader/application/decisions/decision_queries.py").read_text(
        encoding="utf-8"
    )

    assert 'const STATUS_SCHEMA = "runtime_status";' in release_contract
    assert 'const DECISION_VIEW_SCHEMA = "decision_view";' in release_contract
    assert 'DECISION_VIEW_SCHEMA_VERSION = "decision_view"' in decision_queries
    versioned_decision_schema = re.compile(r"\bv\d+_decision_view_v\d+\b", re.IGNORECASE)
    assert versioned_decision_schema.search(release_contract) is None
    assert versioned_decision_schema.search(decision_queries) is None


def test_runtime_and_configuration_paths_are_not_project_versioned() -> None:
    assert (PROJECT_ROOT / "config/runtime.json").is_file()
    assert (PROJECT_ROOT / "config/strategy.json").is_file()
    retired_config_root = PROJECT_ROOT / "config" / ("v" + "2")
    assert not any(path.is_file() for path in retired_config_root.rglob("*"))

    runtime_config = (PROJECT_ROOT / "config/runtime.json").read_text(encoding="utf-8")
    assert '"runtime_dir": ".runtime/trader"' in runtime_config
    versioned_runtime_root = re.compile(r"\.runtime/v\d+", re.IGNORECASE)
    assert versioned_runtime_root.search(runtime_config) is None


def test_baostock_legacy_schema_allowlist_is_explicit_and_bounded() -> None:
    source_paths = (
        PROJECT_ROOT / "src/trader/domain/research/baostock_daily.py",
        PROJECT_ROOT / "src/trader/infra/research/baostock_daily.py",
    )
    token_pattern = re.compile(r'"(baostock_[a-z0-9_]+_v\d+)"')
    tokens = {token for path in source_paths for token in token_pattern.findall(path.read_text(encoding="utf-8"))}
    assert tokens <= LEGACY_BAOSTOCK_PERSISTENCE_SCHEMAS
    assert tokens == LEGACY_BAOSTOCK_PERSISTENCE_SCHEMAS
    assert all("_v4" not in token and "_v5" not in token for token in tokens)
