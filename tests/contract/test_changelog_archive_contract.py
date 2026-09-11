from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"
DELIVERY_LOG = ROOT / "docs/changelog"
ARCHIVE = DELIVERY_LOG / "archive/legacy-through-2026-09-10.md"


def test_root_changelog_is_a_bounded_current_index() -> None:
    content = CHANGELOG.read_text(encoding="utf-8")

    assert content.startswith("# Changelog\n")
    assert "## Unreleased" in content
    assert "docs/changelog/README.md" in content
    assert CHANGELOG.stat().st_size <= 8 * 1024
    assert "zero-argument-history-snapshot-training-alignment" not in content


def test_delivery_archive_preserves_legacy_evidence_and_links() -> None:
    index = (DELIVERY_LOG / "README.md").read_text(encoding="utf-8")
    legacy = ARCHIVE.read_text(encoding="utf-8")

    assert "zero-argument-history-snapshot-training-alignment" in legacy
    assert "score_current_baseline_consistency_audit" in legacy
    for target in re.findall(r"\]\(([^)]+\.md)\)", index):
        assert (DELIVERY_LOG / target).is_file()


def test_current_delivery_records_keep_the_required_audit_sections() -> None:
    records = sorted(path for path in DELIVERY_LOG.glob("*.md") if path.name != "README.md")

    assert records
    for record in records:
        content = record.read_text(encoding="utf-8")
        for section in ("Added", "Changed", "Fixed", "Removed", "Verification", "Residual Risks"):
            assert f"## {section}" in content
