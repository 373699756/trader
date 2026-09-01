import json

from scripts.h1_point_in_time_capability import main


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


def test_script_seals_sanitized_insufficient_terminal_chain_outside_repository(tmp_path, capsys) -> None:
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
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload["status"] == "historical_data_insufficient"
    assert payload["oof_generated"] is False
    assert payload["model_generated"] is False
    assert "masked" not in json.dumps(payload)
    assert (artifacts / "h1_capability_audit.json").is_file()
    assert (artifacts / "historical_label_preregistration.json").is_file()
    assert (artifacts / "codex_a_h1_terminal.json").is_file()
