from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from trader.infra.settings import ConfigurationError, LoadStrategySettings

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("factor", "field", "old_value"),
    (
        ("volume_to_5d_average", "raw_inputs", ["volume_ratio", "intraday_amount", "forward_adjusted_daily_amount"]),
        ("volume_to_5d_average", "formula", "validated realtime volume_ratio when supplied, else intraday_amount/mean(last 5 completed-session daily_amount)"),
        ("unlock_risk", "lookback_window", 90),
        ("unlock_risk", "raw_inputs", ["upcoming_90d_TOTAL_RATIO"]),
        ("reduction_or_unlock", "formula", "max(configured reduction announcement severity,severity(sum(upcoming_90d_TOTAL_RATIO*100),[1,5,10]))"),
        ("negative_announcement_level", "formula", "max configured keyword severity over valid 180d announcements; empty_success=0"),
        ("shareholder_reduction_level", "formula", "controller_or_actual_controller=3; five_percent_holder_or_director=2; other_disclosed_holder=1; terminated_unimplemented=0; source_failure=missing"),
    ),
)
def test_stale_factor_contract_cannot_pass_loading(tmp_path, factor, field, old_value) -> None:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    path = config / "features.json"
    document = json.loads(path.read_text())
    document["factor_index"][factor][field] = old_value
    path.write_text(json.dumps(document))

    with pytest.raises(ConfigurationError, match=rf"{factor}\.{field}"):
        LoadStrategySettings(config / "strategy.json")


def test_unlock_window_contract_tracks_its_actual_company_policy_owner(tmp_path) -> None:
    config = tmp_path / "config"
    shutil.copytree(ROOT / "config", config)
    original = LoadStrategySettings(config / "strategy.json")
    scoring_path = config / "scoring.json"
    scoring = json.loads(scoring_path.read_text())
    scoring["company_scoring"]["unlock_days"] = 90
    scoring_path.write_text(json.dumps(scoring))

    with pytest.raises(ConfigurationError, match=r"unlock_risk\.lookback_window"):
        LoadStrategySettings(config / "strategy.json")

    features_path = config / "features.json"
    features = json.loads(features_path.read_text())
    features["factor_index"]["unlock_risk"]["lookback_window"] = 90
    features_path.write_text(json.dumps(features))
    changed = LoadStrategySettings(config / "strategy.json")

    assert changed.company_scoring.events.unlock_days == 90
    assert changed.strategy_version != original.strategy_version
