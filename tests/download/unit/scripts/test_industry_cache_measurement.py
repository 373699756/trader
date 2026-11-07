from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.download.history import industry_cache_measurement as measurement_module
from scripts.download.history.cli import BuildParser, ParseOptions
from scripts.download.history.commands import BuildCommands
from scripts.download.history.industry_cache_measurement import IndustryCacheMeasurementRequest, MeasureIndustryCache
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.infra.disk_types import DiskProfile


def _arguments(evidence: Path, output: Path, *extra):
    return [
        "--cninfo-cache-evidence", str(evidence), "--cninfo-cache-codes", "002831", "002832",
        "--industry-work-root", str(output), *extra,
    ]


@pytest.mark.parametrize("candidate", ("baseline", "batched", "compare"))
def test_explicit_offline_candidate_uses_existing_worker_without_query_or_dates(tmp_path, candidate):
    options = ParseOptions(BuildParser("history-industry").parse_args(_arguments(
        tmp_path / "input", tmp_path / "output", "--cninfo-cache-candidate", candidate,
    )))
    command, = BuildCommands(options)
    assert options.cninfo_query_codes == () and options.official_announcements == ()
    assert options.cninfo_cache_candidate == candidate
    assert "--cninfo-cache-evidence" in command.argv and "--cninfo-cache-candidate" in command.argv
    assert "--cninfo-query-codes" not in command.argv
    assert command.name == "history_industry"


def test_offline_and_network_source_modes_cannot_be_combined(tmp_path):
    with pytest.raises(SystemExit):
        BuildParser("history-industry").parse_args([
            *_arguments(tmp_path / "input", tmp_path / "output"), "--cninfo-query-codes", "002831", "002832",
        ])


def test_cache_candidate_requires_explicit_existing_evidence_mode():
    with pytest.raises(ValueError, match="requires --cninfo-cache-evidence"):
        ParseOptions(BuildParser("history-industry").parse_args([
            "--official-announcements", "https://www.capco.org.cn/dataFile/zsxfb/hyfl/hyfljg/fixture.js",
            "--industry-work-root", "/tmp/industry-measurement-fixture", "--cninfo-cache-candidate", "batched",
        ]))


def _disk(monkeypatch, kind):
    class Probe:
        def inspect(self, path):
            return DiskProfile(kind, "fixture", "automatic", "unit_test_fixture")

    monkeypatch.setattr(measurement_module, "DiskProbe", Probe)


def test_actual_non_hdd_is_rejected_before_database_creation(tmp_path, monkeypatch):
    _disk(monkeypatch, "ssd")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="physical_hdd"):
        MeasureIndustryCache(IndustryCacheMeasurementRequest(evidence, ("002831", "002832"), output), SqlitePolicyFixture())
    assert not output.exists()


def test_changed_original_is_rejected_without_rewriting_evidence_or_creating_database(tmp_path, monkeypatch):
    _disk(monkeypatch, "hdd")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    expected = b"original supplier bytes"
    digest = hashlib.sha256(expected).hexdigest()
    original = evidence / (digest + ".blob")
    original.write_bytes(b"changed")
    receipt = evidence / "single_002831.receipt.json"
    receipt.write_text(json.dumps({
        "codes": ["002831"], "encoding": "post_query", "from_date": "2018-01-01", "http_status": 200,
        "received_at": "2026-11-07T08:53:15+08:00", "sha256": digest, "to_date": "2026-11-07",
        "url": "https://webapi.cninfo.com.cn/api/stock/p_stock2110",
    }))
    before = receipt.read_bytes()
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="original_changed"):
        MeasureIndustryCache(IndustryCacheMeasurementRequest(evidence, ("002831", "002832"), output), SqlitePolicyFixture())
    assert original.read_bytes() == b"changed" and receipt.read_bytes() == before
    assert not output.exists()


def test_existing_output_is_not_removed_or_reused(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "existing.bin"
    marker.write_bytes(b"retain")
    with pytest.raises(ValueError, match="paths_invalid"):
        MeasureIndustryCache(IndustryCacheMeasurementRequest(evidence, ("002831", "002832"), output), SqlitePolicyFixture())
    assert marker.read_bytes() == b"retain"
