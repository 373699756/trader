from __future__ import annotations

import json

from trader.entrypoints import performance
from trader.recommendation.infra.scoring.profile_factory import ScoringModelsMissingError


def test_missing_model_produces_bounded_performance_report(tmp_path, monkeypatch, capsys) -> None:
    def unavailable(*_args, **_kwargs):
        raise ScoringModelsMissingError()

    monkeypatch.setattr(performance, "Run", unavailable)
    assert performance.Main(["--config", str(tmp_path / "runtime.json"), "--profile", "v2"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["schema_version"] == "production_performance"
    assert report["status"] == "failed"
    assert report["failures"] == ["model_unavailable"]
    assert "private model path" not in captured.out + captured.err
