from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.application.tomorrow_shadow import TomorrowShadowObservation
from trader.entrypoints.cli import main as cli_main
from trader.infra.persistence.tomorrow_shadow_evidence import TomorrowShadowEvidenceRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_cutover_evidence_cli_reports_verified_but_ineligible_window(tmp_path, capsys) -> None:
    runtime_dir = tmp_path / "runtime"
    repository = TomorrowShadowEvidenceRepository(runtime_dir)
    repository.initialize()
    repository.record(_morning_observation())
    config = _runtime_config(tmp_path, runtime_dir)

    assert cli_main(["--config", str(config), "tomorrow-cutover-evidence"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["observation_count"] == 1
    assert report["cutover_status"]["eligible"] is False
    assert cli_main(["--config", str(config), "tomorrow-cutover-evidence", "--require-eligible"]) == 1


def test_cutover_evidence_cli_rejects_missing_database(tmp_path) -> None:
    config = _runtime_config(tmp_path, tmp_path / "missing-runtime")

    with pytest.raises(SystemExit, match="database does not exist"):
        cli_main(["--config", str(config), "tomorrow-cutover-evidence"])


def _runtime_config(tmp_path: Path, runtime_dir: Path) -> Path:
    raw = json.loads((PROJECT_ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    raw["runtime_dir"] = str(runtime_dir)
    raw["strategy_config"] = str(PROJECT_ROOT / "config/v2/strategy.json")
    raw["long_watchlist"] = str(PROJECT_ROOT / "config/v2/long_watchlist.json")
    config = tmp_path / f"runtime-{runtime_dir.name}.json"
    config.write_text(json.dumps(raw), encoding="utf-8")
    return config.resolve()


def _morning_observation() -> TomorrowShadowObservation:
    return TomorrowShadowObservation(
        trade_date=date(2026, 7, 28),
        observed_at=datetime(2026, 7, 28, 9, 40, tzinfo=SHANGHAI),
        baseline_snapshot_id="legacy:morning",
        decision_version="decision:morning",
        input_version="input:morning",
        config_version="runtime:test",
        strategy_version="strategy:test",
        fusion_version="fusion:test",
        decision_schema_version="decision_epoch_v1",
        parent_decision_version="",
        selected_codes_match=True,
        filter_reasons_match=True,
        local_publish_seconds=0.8,
        decision_age_seconds=2.0,
        processing_seconds=0.2,
        deepseek_request_delta=0,
        resource_limits_passed=True,
        baseline_frozen=False,
        v2_frozen=False,
        freeze_codes_match=False,
        freeze_content_hash="",
    )
