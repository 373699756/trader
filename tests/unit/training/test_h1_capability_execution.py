import json
from pathlib import Path

import pytest

from trader.entrypoints.cli import main as cli_main
from trader.training.infra.research.capability_http import BoundedCapabilitySession
from trader.training.infra.research.capability_http import request_params as _request_params


def main(argv, *, session_factory, monkeypatch):
    sample = session_factory()
    monkeypatch.setattr(BoundedCapabilitySession, "get", lambda _self, url, **kw: sample.get(url, **kw))
    config = Path(__file__).resolve().parents[3] / "config/runtime.json"
    return cli_main(["--config", str(config), "research-h1-capability", *argv])


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.content = b"bounded"

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _Session:
    def get(self, url, *, params, timeout):
        del params, timeout
        if "qq.com" in url:
            rows = [[f"2023-01-{index + 1:02d}", "masked"] for index in range(10)]
            return _Response({"code": 0, "data": {"sh600519": {"qfqday": rows}}})
        return _Response({"rc": 0, "data": {"klines": ["2026-09-01 11:20,masked"]}})


class _PartiallyUnavailableSession(_Session):
    def get(self, url, *, params, timeout):
        if "eastmoney" in url:
            raise OSError("bounded supplier failure")
        return super().get(url, params=params, timeout=timeout)


def test_request_params_preserve_supported_supplier_shapes() -> None:
    assert _request_params({"secid": "1.600519", "param": ("sh600519,day",)}) == {
        "secid": "1.600519",
        "param": ("sh600519,day",),
    }
    with pytest.raises(TypeError, match="unsupported H1 capability request parameter"):
        _request_params({"invalid": object()})


def test_script_seals_sanitized_insufficient_terminal_chain_outside_repository(tmp_path, capsys, monkeypatch) -> None:
    archive = tmp_path / "archive"
    artifacts = tmp_path / "artifacts"

    result = main(
        [
            "--h1-runtime-dir",
            str(archive),
            "--artifact-dir",
            str(artifacts),
            "--historical-anchor-date",
            "2022-01-04",
            "--output",
            "-",
        ],
        session_factory=_Session,
        monkeypatch=monkeypatch,
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["schema_version"] == "h1_capability_execution"
    assert payload["status"] == "historical_data_insufficient"
    assert payload["oof_generated"] is False
    assert payload["model_generated"] is False
    assert "masked" not in json.dumps(payload)
    assert (artifacts / "h1_capability_audit.json").is_file()
    assert (artifacts / "historical_label_preregistration.json").is_file()
    assert (artifacts / "h1_research_terminal.json").is_file()


def test_script_reports_partial_probe_failure_without_discarding_success(tmp_path, capsys, monkeypatch) -> None:
    result = main(
        [
            "--h1-runtime-dir",
            str(tmp_path / "archive"),
            "--artifact-dir",
            str(tmp_path / "artifacts"),
            "--output",
            "-",
        ],
        session_factory=_PartiallyUnavailableSession,
        monkeypatch=monkeypatch,
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["schema_version"] == "h1_capability_execution"
    assert payload["status"] == "historical_data_insufficient"
    assert payload["probe_failures"] == ["eastmoney_historical_minute_probe_failed"]
    assert (
        next(item for item in payload["sources"] if item["source"] == "tencent_qfq_daily")["returned_history_rows"]
        == 10
    )
