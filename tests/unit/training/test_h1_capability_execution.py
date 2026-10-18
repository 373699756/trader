import json
from pathlib import Path

import pytest

from trader.entrypoints.cli import Main as cli_main
from trader.infra.http_contracts import EncodeHttpParameters, HttpResponse
from trader.training.infra.research.capability_http import BoundedCapabilitySession


def main(argv, *, session_factory, monkeypatch):
    sample = session_factory()
    monkeypatch.setattr(BoundedCapabilitySession, "get", lambda _self, url, **kw: sample.get(url, **kw))
    config = Path(__file__).resolve().parents[3] / "config/runtime.json"
    return cli_main(["--config", str(config), "research-h1-capability", *argv])


def _response(payload: object) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload).encode())


class _Session:
    def get(self, url, *, params, timeout):
        del params, timeout
        if "qq.com" in url:
            rows = [[f"2023-01-{index + 1:02d}", "masked"] for index in range(10)]
            return _response({"code": 0, "data": {"sh600519": {"qfqday": rows}}})
        return _response({"rc": 0, "data": {"klines": ["2026-09-01 11:20,masked"]}})


class _PartiallyUnavailableSession(_Session):
    def get(self, url, *, params, timeout):
        if "eastmoney" in url:
            raise OSError("bounded supplier failure")
        return super().get(url, params=params, timeout=timeout)


def test_request_params_preserve_supported_supplier_shapes() -> None:
    assert EncodeHttpParameters("https://example.invalid/", {"secid": "1.600519", "param": ("sh600519,day",)}) == (
        "https://example.invalid/?secid=1.600519&param=sh600519%2Cday"
    )
    with pytest.raises(TypeError, match="http_parameter_invalid"):
        EncodeHttpParameters("https://example.invalid/", {"invalid": object()})


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
