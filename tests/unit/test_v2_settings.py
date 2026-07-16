from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.infrastructure.settings import (
    ConfigurationError,
    load_long_watchlist,
    load_runtime_settings,
    load_strategy_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"


def test_v2_configuration_contract_is_valid() -> None:
    runtime = load_runtime_settings(RUNTIME_CONFIG)
    strategy = load_strategy_settings(runtime.strategy_config_path)
    watchlist = load_long_watchlist(runtime.long_watchlist_path)

    assert runtime.schema_version == 2
    assert runtime.runtime_dir == PROJECT_ROOT / ".runtime" / "v2"
    assert sum(runtime.deepseek.strategy_limits.values()) == 188
    assert strategy.fusion.local_weight == pytest.approx(0.68)
    assert strategy.fusion.deepseek_weight == pytest.approx(0.32)
    assert len(watchlist.items) == 10


def test_invalid_strategy_weight_sum_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    source = (PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8")
    strategy_path.write_text(source.replace('"local_weight": 0.68', '"local_weight": 0.5'), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fusion weights"):
        load_strategy_settings(strategy_path)


def test_alternative_fusion_weights_are_rejected_even_when_they_sum_to_one(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["fusion"]["local_weight"] = 0.5
    raw["fusion"]["deepseek_weight"] = 0.5
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="fixed at 0.68 and 0.32"):
        load_strategy_settings(strategy_path)


def test_non_finite_configuration_number_is_rejected(tmp_path) -> None:
    strategy_path = tmp_path / "strategy.json"
    raw = json.loads((PROJECT_ROOT / "config" / "v2" / "strategy.json").read_text(encoding="utf-8"))
    raw["selection"]["observation_margin"] = float("nan")
    strategy_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="finite"):
        load_strategy_settings(strategy_path)
