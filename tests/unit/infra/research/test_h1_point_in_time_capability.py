from datetime import date

import pytest

from trader.infra.research.h1_point_in_time_capability import (
    FreeSourceH1CapabilityProbe,
    H1CapabilityArtifactConflictError,
    H1CapabilityArtifactStore,
)


class _Response:
    def __init__(self, payload: object, size: int = 1000) -> None:
        self._payload = payload
        self.content = b"x" * size

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _Session:
    def get(self, url, *, params, timeout):
        if "qq.com" in url:
            rows = [[f"2023-01-{index + 1:02d}"] for index in range(10)]
            return _Response({"code": 0, "data": {"sh600519": {"qfqday": rows}}})
        return _Response(
            {
                "rc": 0,
                "data": {
                    "klines": [
                        "2026-09-01 11:20,masked",
                        "2026-09-01 14:50,masked",
                    ]
                },
            }
        )


def test_free_source_probe_detects_ignored_old_minute_date_without_exposing_prices() -> None:
    probe = FreeSourceH1CapabilityProbe(_Session(), timeout_seconds=3.0)

    report = probe.run(code="600519", historical_anchor_date=date(2022, 1, 4))

    by_source = {item.source: item for item in report.probes}
    assert by_source["tencent_qfq_daily"].page_size == 10
    minute = by_source["eastmoney_historical_minute"]
    assert minute.earliest_available is None
    assert minute.supports_today_1120 is False
    assert minute.supports_1450 is False
    assert {item.state for item in report.strategies} == {"historical_data_insufficient"}
    assert "masked" not in repr(report)


def test_capability_artifact_is_immutable_and_tamper_evident(tmp_path) -> None:
    report = FreeSourceH1CapabilityProbe(_Session(), timeout_seconds=3.0).run(
        code="600519", historical_anchor_date=date(2022, 1, 4)
    )
    store = H1CapabilityArtifactStore(tmp_path)

    assert store.write(report) == report
    assert store.write(report) == report
    path = tmp_path / "h1_capability_audit.json"
    path.write_text(path.read_text().replace("tencent_qfq_daily", "tampered_source"), encoding="utf-8")
    with pytest.raises(H1CapabilityArtifactConflictError, match="schema or hash"):
        store.verify()
