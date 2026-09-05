import json

from trader.entrypoints.h1_point_in_time import main


def test_h1_audit_command_is_explicit_read_only_and_strategy_scoped(tmp_path, capsys):
    assert main(["--runtime-dir", str(tmp_path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "score_h1_point_in_time_audit"
    assert [item["strategy"] for item in payload["strategies"]] == ["today", "tomorrow", "d25"]
    assert {item["state"] for item in payload["strategies"]} == {"historical_data_insufficient"}
    assert all(item["terminal_holdout_opened"] is False for item in payload["strategies"])
    assert not (tmp_path / "score-h1-point-in-time").exists()
