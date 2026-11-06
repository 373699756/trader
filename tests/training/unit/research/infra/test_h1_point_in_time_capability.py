import json
from datetime import date

import pytest

from trader.download.infra.history_source_capability import FreeHistorySourceCapability
from trader.infra.http_contracts import HttpConnectionError, HttpResponse
from trader.training.infra.research.h1_point_in_time_capability import (
    FreeSourceH1CapabilityProbe,
    H1CapabilityEvidenceConflictError,
    H1CapabilityEvidenceIndex,
)


def _response(payload):
    return HttpResponse(200, json.dumps(payload).encode())


class _Session:
    def get(self, url, *, params, timeout):
        if "qq.com" in url:
            rows = [[f"2023-01-{index + 1:02d}"] for index in range(10)]
            return _response({"code": 0, "data": {"sh600519": {"qfqday": rows}}})
        return _response(
            {
                "rc": 0,
                "data": {
                    "klines": [
                        "2026-09-01 11:20,masked",
                        "2026-09-01 15:00,masked",
                    ]
                },
            }
        )


class _PartiallyUnavailableSession(_Session):
    def get(self, url, *, params, timeout):
        if "eastmoney" in url:
            raise HttpConnectionError("bounded supplier failure")
        return super().get(url, params=params, timeout=timeout)


def test_free_source_probe_detects_ignored_old_minute_date_without_exposing_prices() -> None:
    probe = FreeSourceH1CapabilityProbe(FreeHistorySourceCapability(_Session()), timeout_seconds=3.0)

    report = probe.run(code="600519", historical_anchor_date=date(2022, 1, 4))

    by_source = {item.source: item for item in report.probes}
    assert by_source["tencent_qfq_daily"].page_size == 10
    minute = by_source["eastmoney_historical_minute"]
    assert minute.earliest_available is None
    assert minute.supports_1500 is False
    assert {item.state for item in report.strategies} == {"historical_data_insufficient"}
    assert "masked" not in repr(report)


def test_free_source_probe_preserves_success_when_another_supplier_fails(tmp_path) -> None:
    report = FreeSourceH1CapabilityProbe(
        FreeHistorySourceCapability(_PartiallyUnavailableSession()), timeout_seconds=3.0
    ).run(code="600519", historical_anchor_date=date(2022, 1, 4))

    by_source = {item.source: item for item in report.probes}
    assert by_source["tencent_qfq_daily"].page_size == 10
    assert by_source["eastmoney_historical_minute"].page_size == 0
    assert report.probe_failures == ("eastmoney_historical_minute_probe_failed",)
    assert {item.state for item in report.strategies} == {"historical_data_insufficient"}
    restored = H1CapabilityEvidenceIndex(tmp_path).write(report)
    assert restored.schema_version == "score_h1_source_capability_audit"
    assert restored.probe_failures == report.probe_failures
    assert restored.content_hash == report.content_hash


def test_capability_artifact_is_immutable_and_tamper_evident(tmp_path) -> None:
    report = FreeSourceH1CapabilityProbe(FreeHistorySourceCapability(_Session()), timeout_seconds=3.0).run(
        code="600519", historical_anchor_date=date(2022, 1, 4)
    )
    archive = H1CapabilityEvidenceIndex(tmp_path)

    assert archive.write(report) == report
    assert archive.write(report) == report
    path = tmp_path / "h1_capability_audit.json"
    path.write_text(path.read_text().replace("tencent_qfq_daily", "tampered_source"), encoding="utf-8")
    with pytest.raises(H1CapabilityEvidenceConflictError, match="schema or hash"):
        archive.verify()
