"""Independent expected regions constrain the current-parser diagnostic."""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scripts.download.company.company_repurchase_quality import AuditCompanyRepurchaseQuality
from trader.download.domain.company_events import CompanyEventPolicy
from trader.download.infra.company_document_files import CompanyDocumentFiles
from trader.download.infra.download_settings import LoadCompanySyncConfiguration

_AT = datetime(2026, 11, 17, tzinfo=ZoneInfo("Asia/Shanghai"))
_POLICY = CompanyEventPolicy((0.1, 0.3, 0.5), (5.0, 10.0, 20.0), 365, 90, 180)


def _inputs(tmp_path):
    documents = CompanyDocumentFiles(tmp_path / "text", max_text_characters=10000, max_bytes=1000000, reserve_bytes=1)
    digest = documents.write("证券代码：600001\n本公司保证信息真实。\n累计已回购股数\n10,000股\n累计已回购股数占总股本比例\n0.1%\n")
    target = {
        "code": "600001", "event_id": "original:share_repurchase", "field": "actual_pct", "text_root": str(documents.root),
        "body_hash": digest.hex(), "source": "cninfo", "source_key": "original", "published_at": _AT.isoformat(),
        "title": "关于首次回购公司股份的公告", "url": "", "record_hash": "record",
    }
    target["url"] = "https://example.test/original.pdf"
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"source_publication_hash": "a" * 64, "items": [target]}))
    annotations = {
        "targets_sha256": hashlib.sha256(targets.read_bytes()).hexdigest(),
        "proof_regions": {"original:share_repurchase": [[3, 6]]},
        "items": [{"code": "600001", "event_id": "original:share_repurchase", "field": "actual_pct",
                   "verdict": "prove_current_statement", "expected": 0.1, "lines": [3, 6]}],
    }
    return targets, annotations


def _run(tmp_path, targets, annotations):
    path = tmp_path / "annotations.json"
    path.write_text(json.dumps(annotations))
    return AuditCompanyRepurchaseQuality(
        targets, path, observed_at=_AT, event_policy=_POLICY,
        configuration=LoadCompanySyncConfiguration(Path.cwd()), budget_seconds=20,
    )


def test_exact_independent_region_passes_without_certifying_formal_binding(tmp_path):
    targets, annotations = _inputs(tmp_path)
    before = {path: path.read_bytes() for path in (tmp_path / "text").glob("*/*")}
    report = _run(tmp_path, targets, annotations)
    assert report["status"] == "passed" and report["proved_statements"] == 1
    assert not report["formal_event_binding_verified"] and not report["field_correctness_verified"]
    assert report["source_data_writes"] == report["network_requests"] == 0
    assert before == {path: path.read_bytes() for path in before}


def test_parser_match_outside_the_independent_after_region_is_rejected(tmp_path):
    targets, annotations = _inputs(tmp_path)
    annotations["proof_regions"] = {"original:share_repurchase": [[6, 9]]}
    report = _run(tmp_path, targets, annotations)
    assert report["status"] == "degraded" and report["annotation_matches"] == 0


def test_changed_target_manifest_cannot_reuse_the_independent_annotations(tmp_path):
    targets, annotations = _inputs(tmp_path)
    targets.write_bytes(targets.read_bytes() + b" ")
    with pytest.raises(ValueError, match="annotation_scope_changed"):
        _run(tmp_path, targets, annotations)
