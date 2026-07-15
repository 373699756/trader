import json
import os
import subprocess
import sys


def _load_config_with_environment(**overrides):
    environment = os.environ.copy()
    environment.update(overrides)
    code = """
import json
from stock_analyzer import config
print(json.dumps({
    'debug': config.SERVER_DEBUG,
    'interval': config.VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS,
    'start_time': config.VALIDATION_AUTO_UPDATE_START_TIME,
    'precompute_times': config.DEEPSEEK_PRECOMPUTE_TIMES,
}))
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def test_runtime_environment_overrides_are_typed():
    result = _load_config_with_environment(
        SERVER_DEBUG="off",
        VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS="0",
        VALIDATION_AUTO_UPDATE_START_TIME="15:10",
        DEEPSEEK_PRECOMPUTE_TIMES='["09:45", "10:15"]',
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "debug": False,
        "interval": 0,
        "start_time": "15:10",
        "precompute_times": ["09:45", "10:15"],
    }


def test_invalid_typed_environment_override_fails_at_startup():
    result = _load_config_with_environment(SERVER_DEBUG="sometimes")

    assert result.returncode != 0
    assert "SERVER_DEBUG must be one of" in result.stderr


def test_frozen_production_switch_cannot_be_overridden():
    result = _load_config_with_environment(ENABLE_HISTORY_FACTORS="0")

    assert result.returncode != 0
    assert "ENABLE_HISTORY_FACTORS cannot override the frozen production baseline" in result.stderr


def test_frozen_switch_may_be_repeated_with_its_manifest_value():
    result = _load_config_with_environment(ENABLE_HISTORY_FACTORS="1")

    assert result.returncode == 0, result.stderr
